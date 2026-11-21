# Portal submission — ready to paste

Category: **Builders → Intelligent Contracts**

Below are the exact field texts for the GenLayer Portal contribution form.
Replace `<REPO_URL>` with the public GitHub repository URL once the code is
pushed, and `<STUDIO_CONTRACT_LINK>` with the Studio contract link once
deployed (the form's Link field takes one link — prefer the Studio contract
link if the form asks for the contract, and put the repo link at the end of
the long description; if the form takes the repo, use the repo and keep the
Studio link in the description).

---

## Title

Proof-of-Delivery Escrow — a milestone escrow primitive judged by validator consensus

## Short description

A reusable GenLayer Intelligent Contract that escrows GEN across 2–5
milestones and releases each milestone only when validator consensus agrees
that the payee's delivery evidence satisfies the milestone's numbered
acceptance points. Built for freelance payments, grant tranches, and bounty
payouts — any flow where "the work is done" is a judgment call that should
not rest on one party's say-so.

## Long description

**The problem.** Escrow on deterministic chains works when delivery is a
hash or a token transfer. Most real paid work is not like that: a logo, a
report, a landing page, a research deliverable. Today those payments run on
trust, platform custody, or a single arbiter's availability.

**The primitive.** Proof-of-Delivery Escrow turns "pay when the work is
verifiably done" into a standalone, reusable Intelligent Contract:

- The payer creates an escrow with a named payee, a deadline, an attempt
  budget, and 2–5 milestones. Each milestone has an amount and a
  plain-language acceptance specification written as a **numbered list of
  acceptance points** (structure enforced at creation: 2–8 points,
  sequentially numbered).
- The payer funds the escrow by attaching exactly the total; the contract
  holds the GEN.
- The payee submits delivery evidence (1–5 public URLs + a note) per
  milestone.
- On `review_delivery`, validators **independently fetch the evidence** and
  judge every numbered point with an LLM. Consensus uses a custom
  equivalence rule (`gl.vm.run_nondet_unsafe`): the leader's result stands
  only if each validator's own re-judgment produces the **identical
  categorical verdict AND the identical set of acceptance points met**.
  Free-text reasoning is never compared — verdicts and point-level facts
  are what money moves on. A deterministic consistency gate additionally
  downgrades any "approval" that does not cover every point, so the LLM
  proposes and arithmetic disposes.
- Outcomes are applied by a deterministic state machine only:
  approval releases the milestone into the payee's claimable ledger;
  rejection consumes one attempt and stores the reason (resubmission while
  attempts remain; `DISPUTED` when exhausted, settled by the payer against
  the public verdict history); unreachable evidence yields an
  `INCONCLUSIVE` review that consumes nothing; after the deadline the
  payer can reclaim unreleased milestones, except disputed ones. Payouts
  are ledger-first and collected via `withdraw()` with a native transfer,
  the ledger being zeroed before the transfer is emitted.

**Why it is reusable.** One deployment hosts any number of escrows; the
contract is domain-agnostic (freelance milestones, grant tranche reviews,
bounty judging, procurement acceptance). The numbered-point spec format is
the interface: any frontend or agent framework can generate specs, submit
evidence, and read verdicts (`get_milestone` exposes the full consensus
verdict JSON and the last rejection reason as an audit trail).

**Quality signals.** Readable, commented source following the official
IntelligentOracle conventions (pinned GenVM runner header, defensive LLM
JSON parsing, string-stored enums); a 9-test Direct Mode suite with the
official genlayer-test framework — all passing — covering creation and
funding rules, the consensus happy path, validator disagreement on point
facts (`run_validator()` returning `False`), rejection/resubmission,
dispute exhaustion and resolution in both directions, deadline reclaim
under a warped clock, and inconclusive reviews. The README documents the
state machines, the equivalence rule, Studio deployment steps, and the
primitive's limitations honestly (public-URL evidence, judgment quality
bounded by spec quality, payer as dispute resolver of last resort).

## Links

- Repository: https://github.com/jkislew/proof-of-delivery-escrow
- Contract (GenLayer Studio): 0x8ab30dCcd9809262121838abc07b613730c9Ad05
  — deployed 2026-11-21 from the user's Studio account (0xB1…Cf84)
  after two SDK fixes (added the `# v0.2.17` runner comment and an
  explicit `__init__`, both now in the repo copy; local suite still
  9/9). Note: Studio deploys failed while his external wallet
  (0xc3C3…33Ef) was the active account; switching to a Studio account
  fixed it (proven by the storage.py control test failing the same way).

## Tx hash

Deployment tx: 0xfd3396f9d21e4b1f940a9595b95c49dc4ebbdb17cf935187f2dab19ad57a2e00
(status ACCEPTED in Studio, 2026-11-21).
