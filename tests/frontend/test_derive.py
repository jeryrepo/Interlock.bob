"""tests/frontend/test_derive.py

The frontend projection helpers must never invent a result the backend did
not produce.  These tests pin that behaviour.
"""
import sys
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(FRONTEND))

from utils.derive import (  # noqa: E402
    STATES,
    build_activity_feed,
    coexistence_result,
    contract_test_results,
    critic_assessment,
    hidden_dependencies,
    migration_progress,
    split_consumers,
    state_progress,
    verification_results,
)


def ev(**kwargs):
    base = {
        "claim_type": "dependency",
        "subject": "checkout",
        "content": {},
        "source_ref": "fixtures/x",
        "confidence": "confirmed",
        "created_at": "2026-01-01T10:00:00",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# activity feed
# ---------------------------------------------------------------------------


def test_feed_is_empty_without_evidence():
    assert build_activity_feed([]) == []


def test_confirmed_evidence_reads_as_completed():
    [event] = build_activity_feed([ev()])
    assert event["outcome"] == "completed"
    assert event["level"] == "ok"
    assert event["phase"] == "discovery"


def test_hypothesis_evidence_reads_as_hidden_dependency():
    [event] = build_activity_feed(
        [ev(subject="analytics-worker", confidence="hypothesis")]
    )
    assert event["outcome"] == "hidden dependency discovered"
    assert event["level"] == "alert"


def test_refuted_evidence_is_never_rendered_as_success():
    [event] = build_activity_feed([ev(confidence="refuted")])
    assert event["level"] == "error"
    assert event["outcome"] == "refuted"


def test_migration_plan_evidence_is_phased_as_planning():
    [event] = build_activity_feed(
        [ev(claim_type="migration_status", subject="migration-plan")]
    )
    assert event["phase"] == "planning"


def test_claim_types_map_to_phases():
    events = build_activity_feed(
        [
            ev(claim_type="test_result", subject="checkout"),
            ev(claim_type="risk", subject="critic-assessment"),
            ev(claim_type="migration_status", subject="checkout"),
        ]
    )
    assert [e["phase"] for e in events] == ["verification", "critic", "implementation"]


# ---------------------------------------------------------------------------
# graph projections
# ---------------------------------------------------------------------------


GRAPH = {
    "nodes": [{"id": n, "label": n} for n in
              ["account-service", "checkout", "fraud", "analytics-worker"]],
    "edges": [
        {"from": "account-service", "to": "checkout", "edge_type": "api", "reason": "r"},
        {"from": "account-service", "to": "fraud", "edge_type": "api", "reason": "r"},
        {"from": "account-service", "to": "analytics-worker",
         "edge_type": "undocumented", "reason": "source access"},
    ],
}


def test_split_consumers_separates_undocumented():
    documented, undocumented = split_consumers(GRAPH)
    assert documented == ["checkout", "fraud"]
    assert undocumented == ["analytics-worker"]


def test_split_consumers_handles_missing_graph():
    assert split_consumers(None) == ([], [])


def test_a_consumer_reached_by_both_edge_types_stays_undocumented():
    graph = {
        "nodes": [],
        "edges": GRAPH["edges"]
        + [{"from": "account-service", "to": "analytics-worker",
            "edge_type": "api", "reason": "r"}],
    }
    documented, undocumented = split_consumers(graph)
    assert "analytics-worker" not in documented
    assert "analytics-worker" in undocumented


def test_hidden_dependencies_returns_only_undocumented_edges():
    hidden = hidden_dependencies(GRAPH)
    assert [e["to"] for e in hidden] == ["analytics-worker"]


# ---------------------------------------------------------------------------
# migration + evidence projections
# ---------------------------------------------------------------------------


def test_migration_progress_counts_only_verified():
    gate = {
        "consumers": [
            {"consumer": "checkout", "status": "verified"},
            {"consumer": "fraud", "status": "pending"},
            {"consumer": "analytics-worker", "status": "failed"},
        ]
    }
    assert migration_progress(gate) == (1, 3)


def test_migration_progress_without_gate_is_zero():
    assert migration_progress(None) == (0, 0)


def test_test_results_filters_by_claim_type():
    rows = [ev(claim_type="test_result"), ev(claim_type="risk")]
    assert len(verification_results(rows)) == 1


def test_coexistence_and_critic_are_none_when_absent():
    assert coexistence_result([ev()]) is None
    assert critic_assessment([ev()]) is None


def test_coexistence_and_critic_found_when_present():
    rows = [
        ev(subject="coexistence-rehearsal", claim_type="test_result"),
        ev(subject="critic-assessment", claim_type="risk"),
    ]
    assert coexistence_result(rows)["subject"] == "coexistence-rehearsal"
    assert critic_assessment(rows)["claim_type"] == "risk"


# ---------------------------------------------------------------------------
# state rail
# ---------------------------------------------------------------------------


def test_state_progress_spans_the_workflow():
    assert state_progress("INTAKE") == pytest.approx(1 / len(STATES))
    assert state_progress("DONE") == pytest.approx(1.0)


def test_unknown_state_reports_no_progress():
    assert state_progress("NOPE") == 0.0


# ---------------------------------------------------------------------------
# contract test isolation
# ---------------------------------------------------------------------------


def test_contract_tests_exclude_the_coexistence_rehearsal():
    """
    The rehearsal is a test_result row but reports dual_write_passed, so it
    must not inflate the contract-test count on the passport.
    """
    rows = [
        ev(claim_type="test_result", subject="checkout",
           content={"tests_passed": True, "suite": "contract"}),
        ev(claim_type="test_result", subject="coexistence-rehearsal",
           content={"dual_write_passed": True, "rollback_tested": True}),
    ]
    contract = contract_test_results(rows)
    assert [r["subject"] for r in contract] == ["checkout"]


def test_contract_tests_keep_failing_suites():
    rows = [
        ev(claim_type="test_result", subject="fraud",
           content={"tests_passed": False, "suite": "contract"}),
    ]
    assert len(contract_test_results(rows)) == 1
