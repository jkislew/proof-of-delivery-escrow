"""
Direct-mode tests for the Proof-of-Delivery Escrow contract.

Run from the project root:
    .venv/bin/python -m pytest tests/ -v

Direct mode executes the contract in-process (genlayer-test). Web fetches and
LLM calls are mocked; `direct_vm.run_validator()` replays the captured
validator function so the equivalence rule itself is tested, not just the
leader's outcome.
"""

import json

import pytest

CONTRACT = "contracts/proof_of_delivery_escrow.py"

SPEC_M1 = (
    "1. A public demo page is live at the submitted URL and loads without errors.\n"
    "2. The page shows the final logo in SVG and PNG form."
)
SPEC_M2 = (
    "1. A brand guideline document is delivered as a PDF.\n"
    "2. The document has at least 10 pages covering color and typography."
)

EVIDENCE_URL = "https://evidence.example.com/demo"
EVIDENCE_BODY = (
    "Demo page for the final brand delivery. The page loads cleanly and shows "
    "the final logo in SVG and PNG form, ready for review."
)

APPROVED_JSON = json.dumps(
    {"verdict": "APPROVED", "points_met": [1, 2], "summary": "Every point is met."}
)
REJECTED_JSON = json.dumps(
    {"verdict": "REJECTED", "points_met": [1], "summary": "Point 2 is not shown."}
)


def milestones_json():
    return json.dumps(
        [
            {"title": "Demo page", "amount": 300, "spec": SPEC_M1},
            {"title": "Guideline PDF", "amount": 200, "spec": SPEC_M2},
        ]
    )


def create(direct_vm, direct_deploy, payer, payee, escrow_id="escrow-1",
           deadline="2026-12-31", max_attempts=2):
    contract = direct_deploy(CONTRACT)
    with direct_vm.prank(payer):
        contract.create_escrow(
            escrow_id, payee, "Brand delivery", milestones_json(),
            deadline, max_attempts,
        )
    return contract


def fund(direct_vm, contract, payer, escrow_id="escrow-1", amount=500):
    direct_vm.value = amount
    try:
        with direct_vm.prank(payer):
            contract.fund_escrow(escrow_id)
    finally:
        direct_vm.value = 0


def submit(direct_vm, contract, payee, index=0, escrow_id="escrow-1"):
    with direct_vm.prank(payee):
        contract.submit_delivery(
            escrow_id, index, json.dumps([EVIDENCE_URL]), "Delivery is ready."
        )


def mock_review(direct_vm, llm_response):
    direct_vm.mock_web(
        r"evidence\.example\.com",
        {"method": "GET", "status": 200, "body": EVIDENCE_BODY},
    )
    direct_vm.mock_llm(r"impartial delivery inspector", llm_response)


# ---------------------------------------------------------------------------
# Creation and funding
# ---------------------------------------------------------------------------

def test_create_and_fund(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)

    escrow = contract.get_escrow("escrow-1")
    assert escrow["status"] == "AWAITING_FUNDING"
    assert escrow["total_amount"] == 500
    assert escrow["milestone_count"] == 2
    assert contract.list_escrow_ids() == ["escrow-1"]

    # Funding with the wrong amount is rejected.
    direct_vm.value = 499
    try:
        with direct_vm.prank(direct_alice):
            with direct_vm.expect_revert("must equal the escrow total"):
                contract.fund_escrow("escrow-1")
    finally:
        direct_vm.value = 0

    # Only the payer can fund.
    direct_vm.value = 500
    try:
        with direct_vm.prank(direct_bob):
            with direct_vm.expect_revert("Only the payer"):
                contract.fund_escrow("escrow-1")
    finally:
        direct_vm.value = 0

    fund(direct_vm, contract, direct_alice)
    escrow = contract.get_escrow("escrow-1")
    assert escrow["status"] == "FUNDED"
    assert escrow["funded_amount"] == 500


def test_create_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)

    one_milestone = json.dumps(
        [{"title": "Only one", "amount": 100, "spec": SPEC_M1}]
    )
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("2 to 5 milestones"):
            contract.create_escrow(
                "bad-1", direct_bob, "T", one_milestone, "2026-12-31", 2
            )

    unnumbered = json.dumps(
        [
            {"title": "A", "amount": 100, "spec": "Deliver the thing somehow."},
            {"title": "B", "amount": 100, "spec": SPEC_M2},
        ]
    )
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("numbered acceptance points"):
            contract.create_escrow(
                "bad-2", direct_bob, "T", unnumbered, "2026-12-31", 2
            )

    with direct_vm.prank(direct_alice):
        contract.create_escrow(
            "dup", direct_bob, "T", milestones_json(), "2026-12-31", 2
        )
        with direct_vm.expect_revert("already exists"):
            contract.create_escrow(
                "dup", direct_bob, "T", milestones_json(), "2026-12-31", 2
            )


# ---------------------------------------------------------------------------
# Happy path: consensus approval releases the milestone
# ---------------------------------------------------------------------------

def test_happy_path_release_and_validator_agreement(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)
    fund(direct_vm, contract, direct_alice)

    # The payer cannot submit deliveries; only the payee can.
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("Only the payee"):
            contract.submit_delivery(
                "escrow-1", 0, json.dumps([EVIDENCE_URL]), "mine"
            )

    submit(direct_vm, contract, direct_bob)
    milestone = contract.get_milestone("escrow-1", 0)
    assert milestone["status"] == "UNDER_REVIEW"
    assert contract.get_escrow("escrow-1")["status"] == "IN_PROGRESS"

    mock_review(direct_vm, APPROVED_JSON)
    with direct_vm.prank(direct_charlie):  # anyone may trigger the review
        contract.review_delivery("escrow-1", 0)

    milestone = contract.get_milestone("escrow-1", 0)
    assert milestone["status"] == "RELEASED"
    assert contract.get_claimable(direct_bob) == 300
    assert contract.get_escrow("escrow-1")["released_amount"] == 300

    # The captured validator, re-running the same evaluation independently,
    # agrees with the leader's verdict and point-level facts.
    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_its_facts_differ(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)
    fund(direct_vm, contract, direct_alice)
    submit(direct_vm, contract, direct_bob)

    mock_review(direct_vm, APPROVED_JSON)
    contract.review_delivery("escrow-1", 0)

    # The validator node fetches the same evidence but its judge finds
    # point 2 unmet: verdict and point facts differ -> not equivalent.
    direct_vm.clear_mocks()
    mock_review(direct_vm, REJECTED_JSON)
    assert direct_vm.run_validator() is False


def test_withdraw_pays_out_claimable(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)
    fund(direct_vm, contract, direct_alice)
    submit(direct_vm, contract, direct_bob)
    mock_review(direct_vm, APPROVED_JSON)
    contract.review_delivery("escrow-1", 0)
    assert contract.get_claimable(direct_bob) == 300

    with direct_vm.prank(direct_bob):
        contract.withdraw()
    assert contract.get_claimable(direct_bob) == 0

    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Nothing to withdraw"):
            contract.withdraw()


# ---------------------------------------------------------------------------
# Rejection, resubmission, dispute exhaustion
# ---------------------------------------------------------------------------

def test_rejection_then_resubmission_succeeds(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)
    fund(direct_vm, contract, direct_alice)
    submit(direct_vm, contract, direct_bob)

    mock_review(direct_vm, REJECTED_JSON)
    contract.review_delivery("escrow-1", 0)

    milestone = contract.get_milestone("escrow-1", 0)
    assert milestone["status"] == "PENDING"  # attempts remain: back to pending
    assert milestone["attempts"] == 1
    assert "Point 2" in milestone["last_reason"]
    assert contract.get_claimable(direct_bob) == 0

    # Resubmit and pass on the second attempt.
    submit(direct_vm, contract, direct_bob)
    direct_vm.clear_mocks()
    mock_review(direct_vm, APPROVED_JSON)
    contract.review_delivery("escrow-1", 0)
    assert contract.get_milestone("escrow-1", 0)["status"] == "RELEASED"
    assert contract.get_claimable(direct_bob) == 300


def test_dispute_exhaustion_and_payer_resolution(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = create(
        direct_vm, direct_deploy, direct_alice, direct_bob, max_attempts=1
    )
    fund(direct_vm, contract, direct_alice)

    # Milestone 0: single allowed attempt is rejected by consensus -> DISPUTED.
    submit(direct_vm, contract, direct_bob, index=0)
    mock_review(direct_vm, REJECTED_JSON)
    contract.review_delivery("escrow-1", 0)
    assert contract.get_milestone("escrow-1", 0)["status"] == "DISPUTED"

    # The payer resolves the dispute as a refund to himself.
    with direct_vm.prank(direct_alice):
        contract.resolve_dispute("escrow-1", 0, False)
    assert contract.get_milestone("escrow-1", 0)["status"] == "REFUNDED"
    assert contract.get_claimable(direct_alice) == 300

    # Milestone 1: the payer rejects manually with a reason -> DISPUTED too
    # (max_attempts = 1), then resolves it in the payee's favor.
    submit(direct_vm, contract, direct_bob, index=1)
    with direct_vm.prank(direct_alice):
        contract.reject_delivery("escrow-1", 1, "The PDF link is a placeholder.")
    milestone = contract.get_milestone("escrow-1", 1)
    assert milestone["status"] == "DISPUTED"
    assert milestone["last_reason"] == "The PDF link is a placeholder."

    with direct_vm.prank(direct_alice):
        contract.resolve_dispute("escrow-1", 1, True)
    assert contract.get_milestone("escrow-1", 1)["status"] == "RELEASED"
    assert contract.get_claimable(direct_bob) == 200

    # Everything terminal -> the escrow settles.
    assert contract.get_escrow("escrow-1")["status"] == "SETTLED"


# ---------------------------------------------------------------------------
# Deadline reclaim
# ---------------------------------------------------------------------------

def test_deadline_reclaim(direct_vm, direct_deploy, direct_alice, direct_bob):
    direct_vm.warp("2026-11-21T00:00:00+00:00")
    contract = create(
        direct_vm, direct_deploy, direct_alice, direct_bob, deadline="2026-11-25"
    )
    fund(direct_vm, contract, direct_alice)

    # Before the deadline passes, reclaim is impossible.
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("deadline has not passed"):
            contract.reclaim_expired("escrow-1", 0)

    # After the deadline, an unsubmitted milestone is refunded to the payer.
    direct_vm.warp("2026-11-26T00:00:00+00:00")
    with direct_vm.prank(direct_alice):
        contract.reclaim_expired("escrow-1", 0)
    assert contract.get_milestone("escrow-1", 0)["status"] == "REFUNDED"
    assert contract.get_claimable(direct_alice) == 300

    # The payee cannot reclaim.
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Only the payer"):
            contract.reclaim_expired("escrow-1", 1)


# ---------------------------------------------------------------------------
# Inconclusive reviews do not burn attempts
# ---------------------------------------------------------------------------

def test_inconclusive_review_keeps_attempts(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = create(direct_vm, direct_deploy, direct_alice, direct_bob)
    fund(direct_vm, contract, direct_alice)
    submit(direct_vm, contract, direct_bob)

    # No web mock registered: the evidence fetch fails on every node, the
    # verdict is INCONCLUSIVE, and the milestone stays under review.
    direct_vm.mock_llm(r"impartial delivery inspector", APPROVED_JSON)
    contract.review_delivery("escrow-1", 0)

    milestone = contract.get_milestone("escrow-1", 0)
    assert milestone["status"] == "UNDER_REVIEW"
    assert milestone["attempts"] == 0
