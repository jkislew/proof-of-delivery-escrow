# v0.2.17
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
r"""
Proof-of-Delivery Escrow — a reusable GenLayer Intelligent Contract primitive.

A payer locks GEN into an escrow split into 2-5 milestones. Each milestone has
a plain-language acceptance specification written as a numbered list of
acceptance points. The payee submits delivery evidence (URLs + a note) per
milestone. GenLayer validators then *independently* fetch the evidence and
judge it against the acceptance points with an LLM; consensus is reached with
a custom equivalence rule (see `_evaluate_delivery`):

  * the categorical verdict (APPROVED / REJECTED / INCONCLUSIVE) must match, AND
  * the extracted objective facts — exactly which numbered acceptance points
    are met — must match point for point.

Only deterministic code moves money or state. The LLM never touches the
ledger: it proposes a verdict, validators verify it against independently
fetched evidence, and the state machine below applies the outcome.

State machines
--------------
Escrow:    AWAITING_FUNDING -> FUNDED -> IN_PROGRESS -> SETTLED
Milestone: PENDING -> UNDER_REVIEW -> RELEASED
                                 \-> PENDING   (rejected, attempts remain)
                                 \-> DISPUTED  (rejected, attempts exhausted)
                                 \-> REFUNDED  (deadline passed, reclaimed)
           DISPUTED -> RELEASED | REFUNDED     (payer resolves the dispute)

Value handling: funding is a payable call, so the GEN attached to
`fund_escrow` is held by this contract. Releases credit an internal
`claimable` ledger (so a release is final and auditable even before the
payee collects), and `withdraw` pays the ledger out with a native
`emit_transfer`. Amounts are in wei (u256), mirroring the attached value 1:1.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime

from genlayer import *
import genlayer.gl as gl


# ---------------------------------------------------------------------------
# Storage records. Enum-like values are stored as plain strings because Enum
# payloads are not natively encoded in GenVM storage.
# ---------------------------------------------------------------------------

@allow_storage
@dataclass
class Escrow:
    payer: str            # hex address of the funding party
    payee: str            # hex address of the delivering party
    title: str
    status: str           # AWAITING_FUNDING | FUNDED | IN_PROGRESS | SETTLED
    total_amount: u256    # sum of milestone amounts (wei)
    funded_amount: u256   # value actually attached at funding time
    released_amount: u256 # cumulative amount released to the payee
    refunded_amount: u256 # cumulative amount refunded to the payer
    milestone_count: u256
    max_attempts: u256    # delivery attempts allowed per milestone
    deadline: str         # ISO date (YYYY-MM-DD); reclaim opens the day after
    created_at: str       # ISO date the escrow was created


@allow_storage
@dataclass
class Milestone:
    title: str
    amount: u256
    spec: str             # acceptance spec; numbered points, one per line
    point_count: u256     # number of acceptance points parsed from the spec
    status: str           # PENDING | UNDER_REVIEW | RELEASED | DISPUTED | REFUNDED
    attempts: u256        # rejected submissions so far
    evidence_urls: str    # JSON list of URLs from the latest submission
    delivery_note: str    # payee's note from the latest submission
    verdict_json: str     # JSON of the latest consensus verdict (audit trail)
    last_reason: str      # why the latest submission was rejected (if it was)


# Escrow statuses
_ES_AWAITING_FUNDING = "AWAITING_FUNDING"
_ES_FUNDED = "FUNDED"
_ES_IN_PROGRESS = "IN_PROGRESS"
_ES_SETTLED = "SETTLED"

# Milestone statuses
_MS_PENDING = "PENDING"
_MS_UNDER_REVIEW = "UNDER_REVIEW"
_MS_RELEASED = "RELEASED"
_MS_DISPUTED = "DISPUTED"
_MS_REFUNDED = "REFUNDED"

# Verdicts produced by the non-deterministic evaluation
_V_APPROVED = "APPROVED"
_V_REJECTED = "REJECTED"
_V_INCONCLUSIVE = "INCONCLUSIVE"

_MAX_EVIDENCE_URLS = 5
_MAX_EVIDENCE_CHARS = 6000  # per page, keeps prompts bounded


def _addr_str(addr) -> str:
    """
    Normalize any address representation (Address object, raw 20 bytes, or
    hex string) to the canonical checksummed hex form, so an address stored
    at creation time always matches the same party's sender address later.
    """
    if isinstance(addr, Address):
        return addr.as_hex
    return Address(addr).as_hex


def _parse_acceptance_points(spec: str) -> list[int]:
    """
    Extract the acceptance point numbers from a spec. A valid spec numbers
    its points sequentially starting at 1, one per line, e.g.:

        1. The logo files are delivered in SVG and PNG format.
        2. A brand guideline PDF of at least 10 pages is included.

    The numbering is what makes validator agreement checkable: point N means
    the same sentence for the leader and for every validator.
    """
    numbers = []
    for match in re.finditer(r"(?m)^\s*(\d+)[.)]\s+\S", spec):
        numbers.append(int(match.group(1)))
    return numbers


def _parse_json_dict(raw) -> dict:
    """Defensively extract a JSON object from an LLM response (oracle-style)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    first = raw.find("{")
    last = raw.rfind("}")
    if first == -1 or last == -1 or last < first:
        return {}
    try:
        parsed = json.loads(raw[first:last + 1])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


class ProofOfDeliveryEscrow(gl.Contract):
    # Persistent storage fields
    escrows: TreeMap[str, Escrow]
    milestones: TreeMap[str, Milestone]
    escrow_ids: DynArray[str]
    claimable: TreeMap[str, u256]  # address -> wei withdrawable via withdraw()

    def __init__(self) -> None:
        # No constructor arguments: one deployment hosts any number of
        # escrows, created afterwards via create_escrow(). The explicit
        # __init__ is required by the current SDK schema loader.
        pass

    # ------------------------------------------------------------------
    # Creation and funding
    # ------------------------------------------------------------------

    @gl.public.write
    def create_escrow(
        self,
        escrow_id: str,
        payee: Address,
        title: str,
        milestones_json: str,
        deadline: str,
        max_attempts: u256,
    ) -> None:
        """
        Create an unfunded escrow. `milestones_json` is a JSON list of
        {"title": str, "amount": int (wei), "spec": str} with 2-5 entries.
        Every spec must carry 2-8 sequentially numbered acceptance points.
        """
        if not escrow_id or escrow_id in self.escrows:
            raise gl.vm.UserError("Escrow id is empty or already exists.")
        if not title:
            raise gl.vm.UserError("A title is required.")
        if int(max_attempts) < 1 or int(max_attempts) > 5:
            raise gl.vm.UserError("max_attempts must be between 1 and 5.")
        try:
            deadline_date = datetime.fromisoformat(deadline).date()
        except Exception:
            raise gl.vm.UserError("deadline must be an ISO date (YYYY-MM-DD).")

        specs = _parse_json_dict('{"m": ' + milestones_json + "}").get("m")
        if not isinstance(specs, list) or len(specs) < 2 or len(specs) > 5:
            raise gl.vm.UserError("An escrow needs 2 to 5 milestones.")

        payer = _addr_str(gl.message.sender_address)
        payee_str = _addr_str(payee)
        if payee_str == payer:
            raise gl.vm.UserError("Payer and payee must differ.")

        total = 0
        parsed = []
        for i, m in enumerate(specs):
            if not isinstance(m, dict):
                raise gl.vm.UserError(f"Milestone {i} is not an object.")
            m_title = str(m.get("title", "")).strip()
            m_spec = str(m.get("spec", ""))
            try:
                m_amount = int(m.get("amount", 0))
            except Exception:
                m_amount = 0
            if not m_title:
                raise gl.vm.UserError(f"Milestone {i} has no title.")
            if m_amount <= 0:
                raise gl.vm.UserError(f"Milestone {i} amount must be positive.")
            points = _parse_acceptance_points(m_spec)
            if len(points) < 2 or len(points) > 8:
                raise gl.vm.UserError(
                    f"Milestone {i} spec needs 2 to 8 numbered acceptance points."
                )
            if points != list(range(1, len(points) + 1)):
                raise gl.vm.UserError(
                    f"Milestone {i} acceptance points must be numbered 1..N in order."
                )
            parsed.append((m_title, m_amount, m_spec, len(points)))
            total += m_amount

        self.escrows[escrow_id] = Escrow(
            payer=payer,
            payee=payee_str,
            title=title,
            status=_ES_AWAITING_FUNDING,
            total_amount=u256(total),
            funded_amount=u256(0),
            released_amount=u256(0),
            refunded_amount=u256(0),
            milestone_count=u256(len(parsed)),
            max_attempts=u256(int(max_attempts)),
            deadline=deadline_date.isoformat(),
            created_at=_today_iso(),
        )
        for i, (m_title, m_amount, m_spec, n_points) in enumerate(parsed):
            self.milestones[self._mkey(escrow_id, i)] = Milestone(
                title=m_title,
                amount=u256(m_amount),
                spec=m_spec,
                point_count=u256(n_points),
                status=_MS_PENDING,
                attempts=u256(0),
                evidence_urls="[]",
                delivery_note="",
                verdict_json="",
                last_reason="",
            )
        self.escrow_ids.append(escrow_id)

    @gl.public.write.payable
    def fund_escrow(self, escrow_id: str) -> None:
        """Lock the escrow total. The attached value must equal it exactly."""
        escrow = self._get_escrow(escrow_id)
        if _addr_str(gl.message.sender_address) != escrow.payer:
            raise gl.vm.UserError("Only the payer can fund this escrow.")
        if escrow.status != _ES_AWAITING_FUNDING:
            raise gl.vm.UserError("Escrow is not awaiting funding.")
        if gl.message.value != escrow.total_amount:
            raise gl.vm.UserError(
                "Attached value must equal the escrow total exactly."
            )
        escrow.funded_amount = gl.message.value
        escrow.status = _ES_FUNDED
        self.escrows[escrow_id] = escrow

    # ------------------------------------------------------------------
    # Delivery submission and review
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_delivery(
        self,
        escrow_id: str,
        milestone_index: u256,
        evidence_urls_json: str,
        delivery_note: str,
    ) -> None:
        """Payee submits (or resubmits) delivery evidence for one milestone."""
        escrow = self._get_escrow(escrow_id)
        if _addr_str(gl.message.sender_address) != escrow.payee:
            raise gl.vm.UserError("Only the payee can submit a delivery.")
        if escrow.status not in (_ES_FUNDED, _ES_IN_PROGRESS):
            raise gl.vm.UserError("Escrow is not funded.")
        milestone = self._get_milestone(escrow_id, int(milestone_index))
        if milestone.status != _MS_PENDING:
            raise gl.vm.UserError("Milestone is not open for submission.")
        if int(milestone.attempts) >= int(escrow.max_attempts):
            raise gl.vm.UserError("No delivery attempts remain for this milestone.")

        urls = _parse_json_dict('{"u": ' + evidence_urls_json + "}").get("u")
        if (
            not isinstance(urls, list)
            or len(urls) < 1
            or len(urls) > _MAX_EVIDENCE_URLS
        ):
            raise gl.vm.UserError("Provide 1 to 5 evidence URLs as a JSON list.")
        for url in urls:
            if not isinstance(url, str) or not (
                url.startswith("https://") or url.startswith("http://")
            ):
                raise gl.vm.UserError("Evidence entries must be http(s) URLs.")

        milestone.evidence_urls = json.dumps([str(u) for u in urls])
        milestone.delivery_note = delivery_note
        milestone.status = _MS_UNDER_REVIEW
        self.milestones[self._mkey(escrow_id, int(milestone_index))] = milestone

        if escrow.status == _ES_FUNDED:
            escrow.status = _ES_IN_PROGRESS
            self.escrows[escrow_id] = escrow

    @gl.public.write
    def review_delivery(self, escrow_id: str, milestone_index: u256) -> None:
        """
        Run the validator-consensus review of the current submission and let
        deterministic code apply the agreed outcome. Anyone may call this;
        the outcome depends only on the evidence and the spec.
        """
        escrow = self._get_escrow(escrow_id)
        idx = int(milestone_index)
        milestone = self._get_milestone(escrow_id, idx)
        if milestone.status != _MS_UNDER_REVIEW:
            raise gl.vm.UserError("Milestone is not under review.")

        evidence_urls = json.loads(milestone.evidence_urls)
        verdict = _parse_json_dict(
            self._evaluate_delivery(
                milestone.spec,
                int(milestone.point_count),
                evidence_urls,
                milestone.delivery_note,
            )
        )
        outcome = verdict.get("verdict", _V_INCONCLUSIVE)
        milestone.verdict_json = json.dumps(verdict, sort_keys=True)

        if outcome == _V_APPROVED:
            milestone.status = _MS_RELEASED
            escrow.released_amount = u256(
                int(escrow.released_amount) + int(milestone.amount)
            )
            self._credit(escrow.payee, int(milestone.amount))
        elif outcome == _V_REJECTED:
            self._apply_rejection(escrow, milestone, verdict.get("summary", ""))
        else:
            # INCONCLUSIVE: evidence could not be fetched/verified. The
            # submission stays under review and no attempt is consumed, so a
            # transient web failure can never burn the payee's attempts.
            milestone.last_reason = (
                "Review was inconclusive (evidence unreachable); "
                "call review_delivery again."
            )

        self.milestones[self._mkey(escrow_id, idx)] = milestone
        self._refresh_escrow_status(escrow_id, escrow)

    @gl.public.write
    def reject_delivery(
        self, escrow_id: str, milestone_index: u256, reason: str
    ) -> None:
        """
        Payer-side rejection of a submission under review, with a reason the
        payee can act on. Consumes one delivery attempt, like a validator
        rejection does.
        """
        escrow = self._get_escrow(escrow_id)
        if _addr_str(gl.message.sender_address) != escrow.payer:
            raise gl.vm.UserError("Only the payer can reject a delivery.")
        if not reason:
            raise gl.vm.UserError("A rejection reason is required.")
        idx = int(milestone_index)
        milestone = self._get_milestone(escrow_id, idx)
        if milestone.status != _MS_UNDER_REVIEW:
            raise gl.vm.UserError("Milestone is not under review.")
        self._apply_rejection(escrow, milestone, reason)
        self.milestones[self._mkey(escrow_id, idx)] = milestone
        self._refresh_escrow_status(escrow_id, escrow)

    # ------------------------------------------------------------------
    # Disputes and deadline reclaim
    # ------------------------------------------------------------------

    @gl.public.write
    def resolve_dispute(
        self, escrow_id: str, milestone_index: u256, release_to_payee: bool
    ) -> None:
        """
        Settle a DISPUTED milestone (attempts exhausted). The payer — the
        party whose funds are locked — chooses release or refund. The full
        verdict/rejection history stays on-chain for accountability.
        """
        escrow = self._get_escrow(escrow_id)
        if _addr_str(gl.message.sender_address) != escrow.payer:
            raise gl.vm.UserError("Only the payer can resolve a dispute.")
        idx = int(milestone_index)
        milestone = self._get_milestone(escrow_id, idx)
        if milestone.status != _MS_DISPUTED:
            raise gl.vm.UserError("Milestone is not in dispute.")
        if release_to_payee:
            milestone.status = _MS_RELEASED
            escrow.released_amount = u256(
                int(escrow.released_amount) + int(milestone.amount)
            )
            self._credit(escrow.payee, int(milestone.amount))
        else:
            milestone.status = _MS_REFUNDED
            escrow.refunded_amount = u256(
                int(escrow.refunded_amount) + int(milestone.amount)
            )
            self._credit(escrow.payer, int(milestone.amount))
        self.milestones[self._mkey(escrow_id, idx)] = milestone
        self._refresh_escrow_status(escrow_id, escrow)

    @gl.public.write
    def reclaim_expired(self, escrow_id: str, milestone_index: u256) -> None:
        """
        After the deadline date has passed, the payer reclaims any milestone
        that was never released (still pending or stuck under review).
        Disputed milestones are excluded: they need resolve_dispute, so a
        payer cannot use the deadline to silently bypass a live dispute.
        """
        escrow = self._get_escrow(escrow_id)
        if _addr_str(gl.message.sender_address) != escrow.payer:
            raise gl.vm.UserError("Only the payer can reclaim expired funds.")
        if _today_iso() <= escrow.deadline:
            raise gl.vm.UserError("The deadline has not passed yet.")
        idx = int(milestone_index)
        milestone = self._get_milestone(escrow_id, idx)
        if milestone.status not in (_MS_PENDING, _MS_UNDER_REVIEW):
            raise gl.vm.UserError("Milestone cannot be reclaimed in its state.")
        milestone.status = _MS_REFUNDED
        escrow.refunded_amount = u256(
            int(escrow.refunded_amount) + int(milestone.amount)
        )
        self._credit(escrow.payer, int(milestone.amount))
        self.milestones[self._mkey(escrow_id, idx)] = milestone
        self._refresh_escrow_status(escrow_id, escrow)

    # ------------------------------------------------------------------
    # Payouts
    # ------------------------------------------------------------------

    @gl.public.write
    def withdraw(self) -> None:
        """
        Pay out the caller's claimable ledger balance with a native transfer.
        The ledger is zeroed before the transfer is emitted
        (checks-effects-interactions), so a balance can never be paid twice.
        """
        sender = gl.message.sender_address
        key = _addr_str(sender)
        amount = int(self.claimable[key]) if key in self.claimable else 0
        if amount <= 0:
            raise gl.vm.UserError("Nothing to withdraw.")
        self.claimable[key] = u256(0)
        gl.get_contract_at(sender).emit_transfer(value=u256(amount))

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_escrow(self, escrow_id: str) -> dict:
        e = self._get_escrow(escrow_id)
        return {
            "payer": e.payer,
            "payee": e.payee,
            "title": e.title,
            "status": e.status,
            "total_amount": int(e.total_amount),
            "funded_amount": int(e.funded_amount),
            "released_amount": int(e.released_amount),
            "refunded_amount": int(e.refunded_amount),
            "milestone_count": int(e.milestone_count),
            "max_attempts": int(e.max_attempts),
            "deadline": e.deadline,
            "created_at": e.created_at,
        }

    @gl.public.view
    def get_milestone(self, escrow_id: str, milestone_index: u256) -> dict:
        m = self._get_milestone(escrow_id, int(milestone_index))
        return {
            "title": m.title,
            "amount": int(m.amount),
            "spec": m.spec,
            "point_count": int(m.point_count),
            "status": m.status,
            "attempts": int(m.attempts),
            "evidence_urls": json.loads(m.evidence_urls),
            "delivery_note": m.delivery_note,
            "verdict_json": m.verdict_json,
            "last_reason": m.last_reason,
        }

    @gl.public.view
    def get_claimable(self, address: Address) -> u256:
        key = _addr_str(address)
        return self.claimable[key] if key in self.claimable else u256(0)

    @gl.public.view
    def list_escrow_ids(self) -> list[str]:
        return list(self.escrow_ids)

    # ------------------------------------------------------------------
    # Internal helpers (deterministic)
    # ------------------------------------------------------------------

    def _mkey(self, escrow_id: str, index: int) -> str:
        return escrow_id + "#" + str(index)

    def _get_escrow(self, escrow_id: str) -> Escrow:
        if escrow_id not in self.escrows:
            raise gl.vm.UserError("Unknown escrow id.")
        return self.escrows[escrow_id]

    def _get_milestone(self, escrow_id: str, index: int) -> Milestone:
        escrow = self._get_escrow(escrow_id)
        if index < 0 or index >= int(escrow.milestone_count):
            raise gl.vm.UserError("Milestone index out of range.")
        return self.milestones[self._mkey(escrow_id, index)]

    def _credit(self, address: str, amount: int) -> None:
        current = int(self.claimable[address]) if address in self.claimable else 0
        self.claimable[address] = u256(current + amount)

    def _apply_rejection(self, escrow: Escrow, milestone: Milestone, reason: str) -> None:
        milestone.attempts = u256(int(milestone.attempts) + 1)
        milestone.last_reason = reason
        if int(milestone.attempts) >= int(escrow.max_attempts):
            milestone.status = _MS_DISPUTED
        else:
            milestone.status = _MS_PENDING

    def _refresh_escrow_status(self, escrow_id: str, escrow: Escrow) -> None:
        """An escrow is SETTLED once every milestone is RELEASED or REFUNDED."""
        settled = True
        for i in range(int(escrow.milestone_count)):
            m = self.milestones[self._mkey(escrow_id, i)]
            if m.status not in (_MS_RELEASED, _MS_REFUNDED):
                settled = False
                break
        if settled:
            escrow.status = _ES_SETTLED
        self.escrows[escrow_id] = escrow

    # ------------------------------------------------------------------
    # Non-deterministic core: evidence evaluation under consensus
    # ------------------------------------------------------------------

    def _evaluate_delivery(
        self,
        spec: str,
        point_count: int,
        evidence_urls: list,
        delivery_note: str,
    ) -> str:
        """
        Judge one delivery against its acceptance spec.

        Equivalence rule (the heart of this primitive): the leader fetches
        the evidence and asks an LLM to judge every numbered acceptance
        point. Each validator *independently re-fetches and re-judges*, and
        the leader's result is accepted only if:

          1. the categorical verdicts are identical, and
          2. the sets of acceptance points judged met are identical, and
          3. an INCONCLUSIVE leader result is accepted only if the validator
             also found the evidence unreachable (both sides failed to fetch).

        Free-text reasoning is deliberately NOT compared — wording differs
        between models; verdicts and point-level facts are what money moves
        on, so those are what consensus pins down.
        """

        def judge() -> str:
            pages = []
            fetched = 0
            for url in evidence_urls:
                try:
                    content = gl.nondet.web.render(url, mode="text") or ""
                except Exception:
                    content = ""
                if content.strip():
                    fetched += 1
                pages.append(
                    "<evidence url=\"" + url + "\">\n"
                    + content[:_MAX_EVIDENCE_CHARS]
                    + "\n</evidence>"
                )
            if fetched == 0:
                return json.dumps(
                    {
                        "verdict": _V_INCONCLUSIVE,
                        "points_met": [],
                        "summary": "No evidence page could be fetched.",
                    },
                    sort_keys=True,
                )

            task = f"""
You are an impartial delivery inspector for an escrow contract.
Judge whether the delivered work satisfies the acceptance specification.

### Acceptance specification (numbered points are the contract's terms)
{spec}

### Payee's delivery note
{delivery_note}

### Evidence fetched from the submitted URLs
{chr(10).join(pages)}

### Your task
For EACH numbered acceptance point, decide from the evidence alone whether
it is met. Be strict: a point is met only if the evidence actually shows it.
Then give an overall verdict: APPROVED only if every point is met,
otherwise REJECTED.

### Output format (valid JSON only, no other text)
{{
    "verdict": "APPROVED" or "REJECTED",
    "points_met": [1, 2],
    "summary": "One or two sentences explaining the verdict."
}}
"""
            parsed = _parse_json_dict(gl.nondet.exec_prompt(task))
            verdict = str(parsed.get("verdict", "")).upper()
            if verdict not in (_V_APPROVED, _V_REJECTED):
                return json.dumps(
                    {
                        "verdict": _V_INCONCLUSIVE,
                        "points_met": [],
                        "summary": "The evaluation did not produce a usable verdict.",
                    },
                    sort_keys=True,
                )
            points_met = []
            raw_points = parsed.get("points_met", [])
            if isinstance(raw_points, list):
                for p in raw_points:
                    try:
                        n = int(p)
                    except Exception:
                        continue
                    if 1 <= n <= point_count and n not in points_met:
                        points_met.append(n)
            points_met.sort()
            # Deterministic consistency gate: an approval that does not cover
            # every acceptance point is not an approval, whatever the LLM said.
            if verdict == _V_APPROVED and points_met != list(
                range(1, point_count + 1)
            ):
                verdict = _V_REJECTED
                parsed["summary"] = (
                    "Approval rejected by the consistency gate: "
                    "not every acceptance point was judged met. "
                    + str(parsed.get("summary", ""))
                )
            return json.dumps(
                {
                    "verdict": verdict,
                    "points_met": points_met,
                    "summary": str(parsed.get("summary", ""))[:500],
                },
                sort_keys=True,
            )

        def validator(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            leader = _parse_json_dict(leader_result.calldata)
            if not leader or "verdict" not in leader:
                return False
            try:
                mine = _parse_json_dict(judge())
            except Exception:
                return False
            if leader.get("verdict") != mine.get("verdict"):
                return False
            if leader.get("verdict") == _V_INCONCLUSIVE:
                # Both sides failed to fetch the evidence: agreement.
                return True
            return sorted(leader.get("points_met", [])) == sorted(
                mine.get("points_met", [])
            )

        return str(gl.vm.run_nondet_unsafe(judge, validator))
