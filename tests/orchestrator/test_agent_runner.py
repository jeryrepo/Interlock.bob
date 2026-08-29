"""tests/orchestrator/test_agent_runner.py

Tests for AgentRunner: success, retry, double-failure, and the resumable
stub workflow.
"""
import pytest
from pydantic import ValidationError

from orchestrator.agent_runner import AgentRunner, AgentFailure, run_workflow, STUB_MODE
from orchestrator.schemas import DiscoveryResult
import orchestrator.ledger as ledger
import orchestrator.state_machine as sm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good_discovery(context):
    return DiscoveryResult(
        change_id=context["change_id"],
        evidence=[],
        dependencies=[],
    )


def _bad_once():
    """Returns a callable that fails with a bad dict on attempt 1, succeeds on 2."""
    calls = {"n": 0}

    def fn(context):
        calls["n"] += 1
        if calls["n"] == 1:
            # Return dict missing required field to trigger ValidationError
            return {"change_id": context["change_id"]}  # missing evidence/dependencies
        return _good_discovery(context)

    return fn


def _always_bad(context):
    return {"not_a_valid_key": True}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentRunner:
    def test_successful_run(self, change):
        runner = AgentRunner("test-agent", _good_discovery, DiscoveryResult)
        result = runner.run({"change_id": change["id"]})
        assert isinstance(result, DiscoveryResult)
        assert result.change_id == change["id"]

    def test_retry_on_validation_failure(self, change):
        """Agent fails validation once, succeeds on retry — no AgentFailure raised."""
        runner = AgentRunner("flaky-agent", _bad_once(), DiscoveryResult)
        result = runner.run({"change_id": change["id"]})
        assert isinstance(result, DiscoveryResult)

    def test_double_failure_raises_agent_failure(self, change):
        """Agent fails twice — must raise AgentFailure, never silently swallow."""
        runner = AgentRunner("broken-agent", _always_bad, DiscoveryResult)
        with pytest.raises(AgentFailure, match="broken-agent"):
            runner.run({"change_id": change["id"]})

    def test_accepts_prebuilt_model(self, change):
        """If the agent returns a BaseModel instance already, it is accepted."""
        prebuilt = DiscoveryResult(
            change_id=change["id"], evidence=[], dependencies=[]
        )
        runner = AgentRunner("model-agent", lambda ctx: prebuilt, DiscoveryResult)
        result = runner.run({"change_id": change["id"]})
        assert result is prebuilt


class TestRunWorkflow:
    def test_first_call_stops_at_coordinate(self, conn, change):
        """run_workflow() from INTAKE must stop at COORDINATE — never auto-approve."""
        assert STUB_MODE is True, "Tests require STUB_MODE=True"
        run_workflow(conn, change["id"])
        row = ledger.get_change(conn, change["id"])
        assert row["status"] == "COORDINATE"

    def test_first_call_seeds_evidence(self, conn, change):
        run_workflow(conn, change["id"])
        evidence = ledger.get_evidence(conn, change["id"])
        assert len(evidence) > 0

    def test_first_call_seeds_dependencies_including_analytics_worker(self, conn, change):
        run_workflow(conn, change["id"])
        deps = ledger.get_dependencies(conn, change["id"])
        components = {d["from_component"] for d in deps}
        assert "analytics-worker" in components

    def test_second_call_from_modify_stops_at_approve(self, conn, change):
        """After coordinate approval, run_workflow() from MODIFY stops at APPROVE (gate VERIFIED)."""
        run_workflow(conn, change["id"])  # → COORDINATE
        # Simulate coordinate approval and advance to MODIFY
        ledger.record_approval(conn, change["id"], "coordinate", "tester")
        sm.advance(conn, change["id"])  # COORDINATE → MODIFY
        run_workflow(conn, change["id"])  # → APPROVE
        row = ledger.get_change(conn, change["id"])
        assert row["status"] == "APPROVE"

    def test_second_call_records_gate_decision(self, conn, change):
        run_workflow(conn, change["id"])
        ledger.record_approval(conn, change["id"], "coordinate", "tester")
        sm.advance(conn, change["id"])
        run_workflow(conn, change["id"])
        decision = ledger.get_latest_gate_decision(conn, change["id"])
        assert decision is not None
        assert decision["result"] == "VERIFIED"

    def test_no_auto_approvals_recorded(self, conn, change):
        """run_workflow must never write approval rows — that is the human's job."""
        run_workflow(conn, change["id"])
        approvals = ledger.get_approvals(conn, change["id"])
        assert approvals == []

    def test_second_phase_no_auto_approvals_recorded(self, conn, change):
        run_workflow(conn, change["id"])
        ledger.record_approval(conn, change["id"], "coordinate", "tester")
        sm.advance(conn, change["id"])
        run_workflow(conn, change["id"])
        # Only the manually recorded approval should exist
        approvals = ledger.get_approvals(conn, change["id"])
        gates = {a["gate"] for a in approvals}
        assert gates == {"coordinate"}
        assert "legacy_removal" not in gates

    def test_idempotent_on_non_runnable_state(self, conn, change):
        """Calling run_workflow in a state with no agent work must not raise."""
        sm.force_state(conn, change["id"], "GATE_DECISION")
        run_workflow(conn, change["id"])  # should return silently
        assert sm.get_state(conn, change["id"]) == "GATE_DECISION"
