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
        # Canonical edge: consumer (svc-a) -> provider (account-service)
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


class TestCanonicalEdgeDirection:
    """
    Regression tests verifying correct canonical edge direction.

    Canonical contract: consumer -> provider
      from_component = consumer
      to_component   = provider

    Graph (consumer -> provider direction):
      service-alpha -> account-service   (direct consumer)
      service-beta  -> account-service   (direct consumer)
      service-gamma -> service-beta      (transitive: gamma uses beta's field)

    All three nodes must be affected consumers.

    Migration order: service-gamma must come BEFORE service-beta.
    service-gamma consumes service-beta's field; service-beta cannot drop
    its old field until service-gamma has already migrated away from it.
    So the correct order is: service-gamma, then service-beta (and service-alpha),
    then account-service (last: removes old field once all consumers are done).

    An unrelated node (svc-unrelated, consumer of other-service) must be excluded.
    """

    def test_all_downstream_affected(self, canonical_transitive_input):
        result = run(canonical_transitive_input)
        for consumer in ["service-alpha", "service-beta", "service-gamma"]:
            assert consumer in result["affected_consumers"], (
                f"'{consumer}' should be an affected consumer"
            )

    def test_provider_not_in_affected_consumers(self, canonical_transitive_input):
        result = run(canonical_transitive_input)
        assert "account-service" not in result["affected_consumers"]

    def test_provider_is_first_step(self, canonical_transitive_input):
        result = run(canonical_transitive_input)
        assert components_in_steps(result)[0] == "account-service"

    def test_transitive_consumer_before_its_dependency(self, canonical_transitive_input):
        """
        service-gamma depends on service-beta's field; service-gamma must
        migrate FIRST (before service-beta can drop its old field).
        """
        result = run(canonical_transitive_input)
        steps = components_in_steps(result)
        assert "service-beta" in steps
        assert "service-gamma" in steps
        beta_idx = steps.index("service-beta")
        gamma_idx = steps.index("service-gamma")
        assert gamma_idx < beta_idx, (
            f"service-gamma (idx {gamma_idx}) must precede service-beta (idx {beta_idx}): "
            f"gamma must migrate before beta can remove the old field"
        )

    def test_transitive_before_direct(self, canonical_transitive_input):
        """
        service-gamma is transitive (furthest from provider); it must migrate
        before the direct consumers (service-beta) that it depends on.
        """
        result = run(canonical_transitive_input)
        steps = components_in_steps(result)
        gamma_idx = steps.index("service-gamma")
        assert steps.index("service-beta") > gamma_idx, (
            f"service-gamma must appear before service-beta "
            f"(gamma depends on beta's field and must migrate first)"
        )

    def test_unrelated_component_excluded(self, canonical_transitive_input):
        """
        Add an unrelated edge (other-service -> svc-unrelated) to the input.
        svc-unrelated has no path from account-service and must be excluded.
        """
        data = dict(canonical_transitive_input)
        data["dependencies"] = list(data["dependencies"]) + [
            {"from_component": "other-service", "to_component": "svc-unrelated",
             "edge_type": "api", "reason": None}
        ]
        result = run(data)
        assert "svc-unrelated" not in result["affected_consumers"]
        assert "svc-unrelated" not in components_in_steps(result)
        assert "other-service" not in result["affected_consumers"]

    def test_step_count(self, canonical_transitive_input):
        """1 provider + 3 consumers = 4 steps."""
        result = run(canonical_transitive_input)
        assert len(result["migration_steps"]) == 4

    def test_evidence_source_ref_contains_consumer_name(self, canonical_transitive_input):
        """Evidence for each consumer must mention the consumer in source_ref or content."""
        result = run(canonical_transitive_input)
        for ev in result["evidence"]:
            if ev["claim_type"] == "dependency":
                subject = ev["subject"]
                # source_ref may be a reason string or fallback "dependency:provider->consumer".
                # Either way the consumer subject must appear somewhere in the evidence.
                found_in_ref = subject in ev["source_ref"]
                found_in_content = subject in str(ev.get("content", {}))
                assert found_in_ref or found_in_content, (
                    f"Evidence for '{subject}' does not mention it in source_ref "
                    f"or content: source_ref={ev['source_ref']!r}"
                )
