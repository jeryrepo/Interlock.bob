"""
Tests for agents/planning/compatibility_strategy.py

All inputs are plain dicts — no Pydantic imports.
All tests are pure unit tests: no file I/O, no subprocess calls.

Coverage:
  - basic three-consumer plan
  - unrelated components excluded from steps
  - cycle detection raises ValueError
  - db-edge consumer is last in migration_steps
  - evidence list is non-empty
  - arbitrary consumer names work (proves no hardcoding)
  - analytics-worker absent from input → absent from output
  - verification requirements one-per-consumer
  - no dependencies → ValueError
  - provider absent from graph → ValueError
  - provider step is always first
  - incoming evidence is forwarded in the result
"""

from __future__ import annotations

import pytest

from agents.planning.compatibility_strategy import run


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def components_in_steps(result: dict) -> list[str]:
    return [s["component"] for s in result["migration_steps"]]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicStrategy:
    def test_all_consumers_present(self, three_consumer_input):
        result = run(three_consumer_input)
        components = components_in_steps(result)
        for consumer in ["svc-a", "svc-b", "svc-c"]:
            assert consumer in components, f"{consumer} missing from migration steps"

    def test_provider_is_first_step(self, three_consumer_input):
        result = run(three_consumer_input)
        assert components_in_steps(result)[0] == "account-service"

    def test_affected_consumers_matches_non_provider_steps(self, three_consumer_input):
        result = run(three_consumer_input)
        non_provider = [s["component"] for s in result["migration_steps"] if s["component"] != "account-service"]
        assert set(result["affected_consumers"]) == set(non_provider)

    def test_step_count_is_provider_plus_consumers(self, three_consumer_input):
        result = run(three_consumer_input)
        # 1 provider + 3 consumers
        assert len(result["migration_steps"]) == 4

    def test_each_step_has_required_keys(self, three_consumer_input):
        result = run(three_consumer_input)
        for step in result["migration_steps"]:
            assert "component" in step
            assert "action" in step
            assert "depends_on" in step
            assert "rationale" in step


class TestUnrelatedComponents:
    def test_unrelated_service_excluded(self, unrelated_component_input):
        result = run(unrelated_component_input)
        components = components_in_steps(result)
        assert "svc-x" not in components, "svc-x has no path to provider; must not appear"

    def test_related_service_included(self, unrelated_component_input):
        result = run(unrelated_component_input)
        assert "svc-a" in components_in_steps(result)


class TestCycleDetection:
    def test_cycle_raises_value_error(self, cyclic_input):
        with pytest.raises(ValueError, match="cycle"):
            run(cyclic_input)


class TestDbEdgeConsumer:
    def test_db_consumer_is_last_non_provider_step(self, with_db_consumer_input):
        result = run(with_db_consumer_input)
        steps = result["migration_steps"]
        # Remove the provider step (always first)
        consumer_steps = [s["component"] for s in steps[1:]]
        assert consumer_steps[-1] == "platform-cfg", (
            f"db-edge consumer 'platform-cfg' should be last; got order: {consumer_steps}"
        )

    def test_api_consumers_before_db_consumer(self, with_db_consumer_input):
        result = run(with_db_consumer_input)
        consumer_steps = [s["component"] for s in result["migration_steps"][1:]]
        db_idx = consumer_steps.index("platform-cfg")
        for c in ["svc-a", "svc-b"]:
            assert consumer_steps.index(c) < db_idx, f"{c} should come before platform-cfg"


class TestEvidence:
    def test_evidence_list_non_empty(self, three_consumer_input):
        result = run(three_consumer_input)
        assert len(result["evidence"]) > 0

    def test_evidence_entries_have_required_fields(self, three_consumer_input):
        result = run(three_consumer_input)
        for ev in result["evidence"]:
            assert ev.get("claim_type") in {"dependency", "migration_status", "test_result", "risk"}
            assert "subject" in ev
            assert "source_ref" in ev
            assert ev.get("confidence") in {"hypothesis", "confirmed", "refuted"}

    def test_incoming_evidence_forwarded(self):
        """Incoming evidence from discovery must appear in the result."""
        incoming = {
            "claim_type": "dependency",
            "subject": "svc-a",
            "content": {"note": "discovered from source scan"},
            "source_ref": "fixtures/svc-a/worker.py:42",
            "confidence": "confirmed",
            "source_revision": "abc1234",
        }
        data = {
            "change_request": {"id": "cr-x", "old_field": "customer_id", "new_field": "account_id", "provider": "account-service"},
            "dependencies": [{"from_component": "svc-a", "to_component": "account-service", "edge_type": "api", "reason": None}],
            "evidence": [incoming],
        }
        result = run(data)
        assert incoming in result["evidence"]


class TestNoHardcoding:
    def test_analytics_worker_absent_when_not_in_graph(self, no_analytics_worker_input):
        """
        If analytics-worker is not in the dependency graph, it must not appear
        in migration steps. This test would fail if the agent hardcoded that name.
        """
        result = run(no_analytics_worker_input)
        components = components_in_steps(result)
        assert "analytics-worker" not in components

    def test_arbitrary_consumer_names_work(self, arbitrary_names_input):
        """
        Use entirely arbitrary provider and consumer names.
        Any hardcoding of real service names would cause this test to fail.
        """
        result = run(arbitrary_names_input)
        components = components_in_steps(result)
        assert "zeta-core" in components, "Provider must be first step"
        assert components[0] == "zeta-core"
        for consumer in ["omega-ui", "gamma-worker", "delta-cfg"]:
            assert consumer in components, f"{consumer} not in migration steps"

    def test_arbitrary_names_db_last(self, arbitrary_names_input):
        result = run(arbitrary_names_input)
        consumer_steps = [s["component"] for s in result["migration_steps"][1:]]
        assert consumer_steps[-1] == "delta-cfg"

    def test_interlock_names_absent_from_arbitrary_result(self, arbitrary_names_input):
        """No Interlock-specific names should bleed into an arbitrary-name plan."""
        result = run(arbitrary_names_input)
        all_text = str(result["migration_steps"])
        for hardcoded in ("checkout", "fraud", "analytics-worker", "account-service"):
            assert hardcoded not in all_text, f"Hardcoded name '{hardcoded}' leaked into result"


class TestVerificationRequirements:
    def test_one_requirement_per_consumer(self, three_consumer_input):
        result = run(three_consumer_input)
        consumers = result["affected_consumers"]
        reqs = result["verification_requirements"]
        assert len(reqs) == len(consumers), (
            f"Expected {len(consumers)} verification requirements; got {len(reqs)}"
        )

    def test_each_consumer_mentioned_in_requirements(self, three_consumer_input):
        result = run(three_consumer_input)
        for consumer in result["affected_consumers"]:
            assert any(consumer in req for req in result["verification_requirements"]), (
                f"'{consumer}' not mentioned in any verification requirement"
            )

    def test_new_field_mentioned_in_requirements(self, three_consumer_input):
        result = run(three_consumer_input)
        cr = three_consumer_input["change_request"]
        for req in result["verification_requirements"]:
            assert cr["new_field"] in req


class TestCompatibilityRequirements:
    def test_old_and_new_field_in_compat_requirements(self, three_consumer_input):
        result = run(three_consumer_input)
        cr = three_consumer_input["change_request"]
        full_text = " ".join(result["compatibility_requirements"])
        assert cr["old_field"] in full_text
        assert cr["new_field"] in full_text

    def test_provider_name_in_compat_requirements(self, three_consumer_input):
        result = run(three_consumer_input)
        provider = three_consumer_input["change_request"]["provider"]
        full_text = " ".join(result["compatibility_requirements"])
        assert provider in full_text


class TestErrorHandling:
    def test_missing_dependencies_raises(self):
        data = {
            "change_request": {"id": "x", "old_field": "a", "new_field": "b", "provider": "p"},
            "dependencies": [],
            "evidence": [],
        }
        with pytest.raises(ValueError, match="[Nn]o dependencies"):
            run(data)

    def test_none_dependencies_raises(self):
        data = {
            "change_request": {"id": "x", "old_field": "a", "new_field": "b", "provider": "p"},
            "dependencies": None,
            "evidence": [],
        }
        with pytest.raises(ValueError, match="[Nn]o dependencies"):
            run(data)

    def test_provider_absent_from_graph_raises(self):
        data = {
            "change_request": {"id": "x", "old_field": "a", "new_field": "b", "provider": "missing-provider"},
            "dependencies": [{"from_component": "svc-a", "to_component": "some-other", "edge_type": "api", "reason": None}],
            "evidence": [],
        }
        with pytest.raises(ValueError, match="missing-provider"):
            run(data)


class TestNoConsumers:
    def test_provider_only_plan(self, no_consumers_input):
        """When nothing depends on the provider, steps contain only the provider."""
        result = run(no_consumers_input)
        assert components_in_steps(result) == ["account-service"]
        assert result["affected_consumers"] == []
        assert result["verification_requirements"] == []
