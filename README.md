# Proof-of-Delivery Escrow

A reusable **milestone escrow primitive** for GenLayer Intelligent Contracts.

A payer locks GEN into an escrow split into 2–5 milestones. Each milestone
carries a plain-language **acceptance specification** — a numbered list of
acceptance points. The payee delivers work off-chain and submits evidence
(URLs + a short note). GenLayer validators then **independently fetch the
evidence and judge it against the acceptance points**, and only deterministic
code moves the money.

This is the missing settlement layer for any "pay when the work is verifiably
done" flow where "done" is a judgment call, not a hash.

## Who reuses this

- **Freelance / agency payments** — clients fund milestones up front; the
  freelancer gets paid when delivery evidence passes review, not when the
  client feels like releasing funds.
- **Grant programs** — foundations release tranches when grantees' progress
  reports satisfy published acceptance criteria, with an on-chain audit trail
  of every verdict.
- **Bounty payouts** — a bounty poster escrows the prize against acceptance
  criteria; submissions are judged by validator consensus instead of one
  maintainer's availability.
- **Procurement / OTC service deals** — any two parties who want a neutral,
  programmable referee for subjective deliverables.

Deploy one contract and create any number of escrows inside it, or deploy a
fresh instance per agreement — the contract holds no global state beyond the
escrows themselves and a payout ledger.

## How it works

### Roles and flow

1. **Create** — the payer defines an escrow: payee, title, deadline, max
   delivery attempts per milestone (1–5), and 2–5 milestones, each with an
   amount (wei) and an acceptance spec whose points are numbered `1.` `2.` …
   (2–8 points per milestone; numbering is validated at creation).
2. **Fund** — the payer calls `fund_escrow` attaching exactly the escrow
   total. The GEN is held by the contract.
3. **Deliver** — the payee calls `submit_delivery` with 1–5 evidence URLs
   and a note. The milestone moves to `UNDER_REVIEW`.
4. **Review** — anyone calls `review_delivery`. Validators fetch the
   evidence and judge it (see *Consensus* below). The consensus outcome is
   applied by deterministic code:
   - **APPROVED** → milestone `RELEASED`; the amount is credited to the
     payee's claimable balance.
   - **REJECTED** → one attempt is consumed and the reason is stored. With
     attempts left, the milestone returns to `PENDING` for resubmission;
     with none left, it becomes `DISPUTED`.
   - **INCONCLUSIVE** (evidence unreachable) → nothing is consumed; the
     review can simply be called again. A flaky URL can never burn attempts.
5. **Payer rejection** — while a submission is under review, the payer may
   reject it directly with a mandatory written reason (`reject_delivery`).
   This consumes an attempt exactly like a validator rejection, so the payer
   cannot grief the payee with infinite free rejections.
6. **Disputes** — a `DISPUTED` milestone is settled by the payer via
   `resolve_dispute` (release to payee or refund to payer). The complete
   verdict and rejection history stays on-chain, so the decision is made
   against a public record.
7. **Deadline** — after the deadline date, the payer can reclaim any
   milestone that was never released (`reclaim_expired`). Disputed
   milestones are excluded — a deadline cannot be used to silently bypass a
   live dispute.
8. **Withdraw** — releases and refunds credit an internal `claimable`
   ledger; `withdraw` pays a caller's balance out with a native transfer.
   The ledger is zeroed before the transfer is emitted, so no balance can
   ever be paid twice.

### State machines

```
Escrow:    AWAITING_FUNDING ──fund──▶ FUNDED ──first submission──▶ IN_PROGRESS
                                                                      │
                                          all milestones terminal ──▶ SETTLED

Milestone: PENDING ──submit──▶ UNDER_REVIEW ──approved──▶ RELEASED
                                  │  │
                                  │  └─rejected, attempts left──▶ PENDING
                                  └─rejected, attempts exhausted──▶ DISPUTED
           DISPUTED ──resolve_dispute──▶ RELEASED | REFUNDED
           PENDING / UNDER_REVIEW ──deadline passed + reclaim──▶ REFUNDED
```

### Consensus: the equivalence rule

Judging "did they deliver what was promised?" is non-deterministic — two
honest reviewers phrase their reasoning differently. This contract therefore
never compares free text. It pins consensus to **decision fields**:

- The leader fetches every evidence page (`gl.nondet.web.render`) and asks
  an LLM to judge **each numbered acceptance point** as met or not met, and
  to return a categorical verdict as JSON.
- Each validator **independently re-fetches the same evidence and re-runs
  the same judgment** (custom validator via `gl.vm.run_nondet_unsafe`,
  following the docs' "independent verification is required" rule — a
  validator that only checks the leader's JSON shape is not consensus).
- The leader's result is accepted only if **the verdicts are identical AND
  the sets of points judged met are identical**. An `INCONCLUSIVE` result
  (evidence unreachable) is accepted only if the validator also failed to
  fetch — one side's network hiccup cannot masquerade as a judgment.
- A deterministic **consistency gate** runs after parsing: an `APPROVED`
  verdict whose point set does not cover every acceptance point is
  downgraded to `REJECTED` by plain code. The LLM proposes; arithmetic
  disposes.

Because acceptance points are numbered at creation time, "point 2" denotes
the same sentence for the leader, every validator, the payer, and the
payee — which is what makes point-level agreement a meaningful, checkable
fact instead of a vibe.

## Contract API

| Method | Type | Caller | Purpose |
| --- | --- | --- | --- |
| `create_escrow(escrow_id, payee, title, milestones_json, deadline, max_attempts)` | write | payer | Define an escrow (unfunded). `milestones_json`: JSON list of `{title, amount, spec}` |
| `fund_escrow(escrow_id)` | write, payable | payer | Lock exactly the escrow total |
| `submit_delivery(escrow_id, milestone_index, evidence_urls_json, delivery_note)` | write | payee | Submit/resubmit evidence (JSON list of 1–5 URLs) |
| `review_delivery(escrow_id, milestone_index)` | write | anyone | Run consensus review and apply the outcome |
| `reject_delivery(escrow_id, milestone_index, reason)` | write | payer | Reject a submission with a reason (consumes an attempt) |
| `resolve_dispute(escrow_id, milestone_index, release_to_payee)` | write | payer | Settle a disputed milestone |
| `reclaim_expired(escrow_id, milestone_index)` | write | payer | Refund an unreleased milestone after the deadline |
| `withdraw()` | write | anyone | Pay out the caller's claimable balance |
| `get_escrow(escrow_id)` | view | — | Escrow state incl. released/refunded totals |
| `get_milestone(escrow_id, milestone_index)` | view | — | Milestone state, evidence, last verdict JSON, last rejection reason |
| `get_claimable(address)` | view | — | Withdrawable ledger balance |
| `list_escrow_ids()` | view | — | All escrow ids in creation order |

Amounts are `u256` wei and mirror the GEN attached at funding 1:1.

## Deploy on GenLayer Studio (Studio Next / Bradbury)

1. Open GenLayer Studio and create a new contract file with the contents of
   `contracts/proof_of_delivery_escrow.py`. Keep the first line intact — it
   pins the GenVM runner (`py-genlayer:1jb45aa8…`); unversioned `test` /
   `latest` aliases are rejected by the network.
2. Deploy the contract (constructor takes no arguments). Fund your Studio
   account from the testnet faucet first if needed.
3. Create an escrow from the payer account, e.g.
   `create_escrow("logo-job-1", <payee address>, "Logo delivery",
   "[{\"title\": \"Demo page\", \"amount\": 300, \"spec\": \"1. ...\\n2. ...\"}, …]",
   "2026-12-31", 2)`.
4. Call `fund_escrow("logo-job-1")` with the transaction value set to the
   exact milestone total.
5. As the payee, `submit_delivery(...)` with public evidence URLs; then any
   account calls `review_delivery(...)` and validators reach consensus on
   the verdict. Read the outcome with `get_milestone` / `get_claimable`,
   and collect with `withdraw()`.

## Tests

The suite uses the official **genlayer-test** framework in **Direct Mode**
(in-process execution, mocked web/LLM, captured-validator replay):

```bash
python3 -m venv .venv
.venv/bin/pip install genlayer-test
.venv/bin/python -m pytest tests/ -v
```

Current status: **9 tests, all passing** (verified locally 2026-11-21,
genlayer-test 0.29.2, genvm v0.2.12 runner). Coverage:

- escrow creation + validation (milestone count, numbered-point specs,
  duplicate ids) and exact-value funding with payer-only access control;
- the happy path: submission → consensus approval → release → claimable
  credit, plus `run_validator()` confirming the validator **agrees** with
  the leader under identical evidence;
- a validator whose judge extracts different point facts **disagrees**
  (`run_validator()` returns `False`) — the equivalence rule itself is
  under test, not just the happy outcome;
- withdraw paying out and refusing a second payout;
- validator rejection → resubmission → approval;
- attempt exhaustion → `DISPUTED`, payer dispute resolution in both
  directions (refund and release), escrow reaching `SETTLED`;
- deadline reclaim blocked before the deadline and honored after it
  (time controlled with the `warp` cheatcode);
- unreachable evidence producing an `INCONCLUSIVE` review that consumes
  no attempt.

Note: Direct Mode downloads the GenVM runner/SDK matching the contract's
pinned header on first run (cached under `~/.cache/gltest-direct`). If the
newest genvm release has renamed its release assets, seed the cache with
the `genvm-universal.tar.xz` from the v0.2.12 release, which contains this
runner. Studio Mode (`gltest` CLI against a local Studio) can run the same
scenarios end-to-end with real multi-validator consensus.

## Design notes and prior art

Patterns deliberately borrowed from the official material:

- **IntelligentOracle** (genlayerlabs/intelligent-oracle): pinned runner
  header, defensive JSON extraction from LLM output, `gl.vm.UserError` for
  contract-level reverts, string-stored status enums.
- **GenLayer docs — The Equivalence Principle**: custom validator with
  `run_nondet_unsafe`, partial-field matching on decision fields,
  independent re-execution instead of leader-output-only checks, explicit
  handling of `gl.vm.Return` vs error results.
- **GenLayer docs — Testing Intelligent Contracts**: layered tests (pure
  storage first, mocked non-determinism second, consensus replay third).

## Limitations (stated plainly)

- **Evidence is public URLs.** Validators must be able to fetch the
  evidence, so private or access-controlled deliverables need a public
  snapshot/summary page. Evidence pages are truncated to 6,000 characters
  per page in the judging prompt.
- **Judgment quality is model quality.** The equivalence rule guarantees
  validators *agree* on verdict and point facts; it cannot guarantee the
  shared judgment is wise. Tight, objectively checkable acceptance points
  ("the PDF has at least 10 pages") judge far more reliably than taste
  ("the design feels premium"). Write specs accordingly — the contract
  enforces structure, not wisdom.
- **The payer is the dispute resolver of last resort.** After attempts are
  exhausted the payer decides release vs refund. This mirrors real escrow
  practice (the funding party arbitrates) and the full review history is
  public, but a deployment that needs neutral arbitration can subclass
  `resolve_dispute` to a third-party arbiter address — the state machine
  does not change.
- **Deadline granularity is one calendar day** (dates, not timestamps), and
  amounts are raw wei — frontends should format both for humans.
- **Payouts are ledger-first.** Releases credit the internal `claimable`
  ledger and are paid out on `withdraw()` via a native emitted transfer on
  finalization; integrators should surface "claimable" balances in their UI
  rather than expecting instant transfers at review time.
