"""
tests/orchestrator/test_real_workflow.py
=========================================
Tests for the real-agent path: adapters, the registry, the generalised gate,
and one genuine end-to-end run.

The adapter tests are pure — recorded agent output in, orchestrator schema out,
no database and no subprocess. The end-to-end test is marked `integration`
because it copies the fixture tree, runs real pytest and makes real git commits;
it needs no network and no Docker, so it runs by default.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import orchestrator.adapters as adapters
import orchestrator.gate as gate
import orchestrator.ledger as ledger
import orchestrator.main as main
from orchestrator.agent_registry import AGENT_REGISTRY, agents_for, make_callable
from orchestrator.schemas import (
    CHANGE_KINDS,
    ImplementationResult,
    PlanningResult,
    symbols_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

FIELD_RENAME_SPEC = {
    "kind": "field_rename",
    "provider": "account-service",
    "components_root": "fixtures",
    "old_field": "customer_id",
    "new_field": "account_id",
}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class TestPlanningAdapter:
    def test_affected_consumers_becomes_migration_order(self):
        raw = {"affected_consumers": ["checkout", "fraud"], "evidence": []}
        out = adapters.planning(raw, {"change_id": "c1"})
        assert PlanningResult(**out).migration_order == ["checkout", "fraud"]

    def test_derived_reasoning_is_preserved_as_evidence(self):
        """
        migration_steps and the requirement lists have no home in PlanningResult.
        Dropping them would discard real derived reasoning, so they must survive
        as evidence.
        """
        raw = {
            "affected_consumers": ["checkout"],
            "migration_steps": [{"component": "checkout", "action": "migrate"}],
            "compatibility_requirements": ["dual-write both fields"],
            "verification_requirements": ["checkout must pass"],
            "evidence": [],
        }
        out = adapters.planning(raw, {"change_id": "c1"})
        plan_ev = [e for e in out["evidence"] if e["subject"] == "migration-plan"]
        assert len(plan_ev) == 1
        content = plan_ev[0]["content"]
        assert content["migration_steps"] == raw["migration_steps"]
        assert content["compatibility_requirements"] == raw["compatibility_requirements"]

    def test_plan_evidence_carries_no_commit(self):
        """A plan describes intent, not code, so it legitimately has no SHA."""
        out = adapters.planning({"affected_consumers": []}, {"change_id": "c1"})
        plan_ev = [e for e in out["evidence"] if e["subject"] == "migration-plan"][0]
        assert plan_ev["source_revision"] is None


class TestImplementationAdapter:
    def test_commit_sha_becomes_commit_ref(self):
        raw = {
            "consumer": "checkout", "repository": "/tmp/checkout",
            "commit_sha": "a" * 40, "evidence": [], "status": "success",
        }
        out = adapters.implementation(raw, {"change_id": "c1"})
        assert ImplementationResult(**out).commit_ref == "a" * 40

    def test_provider_patch_consumer_falls_back_to_repository_name(self):
        """provider_patch returns no `consumer` key — only `repository`."""
        raw = {
            "repository": "/work/account-service", "commit_sha": "b" * 40,
            "evidence": [], "status": "success",
        }
        out = adapters.implementation(raw, {"change_id": "c1"})
        assert out["consumer"] == "account-service"

    @pytest.mark.parametrize("status", ["failed", "error", None, ""])
    def test_a_failed_agent_never_becomes_a_successful_result(self, status):
        """
        The single most important line in adapters.py. Mapping a failure onto a
        well-formed ImplementationResult would let unverified code past the gate
        (AGENTS.md invariant 4).
        """
        raw = {"consumer": "checkout", "commit_sha": None,
               "evidence": [], "status": status}
        with pytest.raises(ValueError):
            adapters.implementation(raw, {"change_id": "c1", "role": "provider-patch"})


class TestVerificationAdapter:
    def test_failed_status_is_passed_through_untouched(self):
        raw = {"consumer": "checkout", "status": "failed", "evidence": []}
        out = adapters.verification(raw, {"change_id": "c1"})
        assert out["status"] == "failed"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_every_change_kind_is_registered(self):
        for kind in CHANGE_KINDS:
            assert agents_for(kind, "DISCOVERY"), f"{kind} has no discovery agents"
            assert agents_for(kind, "PLANNING"), f"{kind} has no planning agent"

    def test_unknown_kind_yields_no_agents_rather_than_raising(self):
        assert agents_for("no_such_kind", "DISCOVERY") == ()

    def test_api_contract_change_skips_db_discovery(self):
        """An API contract change does not live in SQL."""
        roles = {a.role for a in agents_for("api_contract_change", "DISCOVERY")}
        assert "db-schema-discovery" not in roles

    def test_transport_migration_uses_the_subscribe_step(self):
        steps = {a.step_kind for a in agents_for("transport_migration", "MODIFY")}
        assert "subscribe" in steps

    def test_registry_import_paths_all_resolve(self):
        """A typo in an import_path must not wait until runtime to surface."""
        import importlib

        for agents in AGENT_REGISTRY.values():
            for spec in agents:
                module = importlib.import_module(spec.import_path)
                assert hasattr(module, "run"), f"{spec.import_path} has no run()"

    def test_signature_normalisation_passes_only_declared_arguments(self):
        """
        Agents disagree about their signatures; AgentRunner passes one dict.
        The shim must bridge that without widening AgentRunner.
        """
        from orchestrator.agent_registry import _bind

        def data_only(data):
            return {"got": list(data)}

        def with_repo(data, repo_path):
            return {"repo": str(repo_path)}

        def with_base(data, base_url):
            return {"base": base_url}

        ctx = {"data": {"x": 1}, "repo_path": "/tmp/x", "base_url": "http://h"}
        assert _bind(data_only, ctx) == {"got": ["x"]}
        assert _bind(with_repo, ctx)["repo"].endswith("x")
        assert _bind(with_base, ctx) == {"base": "http://h"}


# ---------------------------------------------------------------------------
# Gate generalisation
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    return ledger.init_db(":memory:")


class TestGateGeneralisation:
    def test_legacy_change_uses_the_fallback_provider(self, conn):
        """No spec means the pre-existing constant, so old verdicts are unchanged."""
        ledger.create_change(conn, "c1", "legacy")
        assert gate._resolve_provider(conn, "c1") == gate.PROVIDER
        assert gate._resolve_step_kinds(conn, "c1") == ("migrate",)
        assert gate._resolve_provider_steps(conn, "c1") == ()

    def test_provider_comes_from_the_spec(self, conn):
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename",
                               {**FIELD_RENAME_SPEC, "provider": "billing"})
        assert gate._resolve_provider(conn, "c1") == "billing"

    def test_unknown_kind_fails_closed(self, conn):
        """An unrecognised kind must get the strictest known set, never empty."""
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "invented_kind", {"provider": "p"})
        assert gate._resolve_step_kinds(conn, "c1") == gate._DEFAULT_STEP_KINDS

    def test_transport_migration_requires_two_steps(self, conn):
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "transport_migration", {"provider": "billing"})
        ledger.add_dependency(conn, "c1", "billing", "notifier", "event", None)

        assert gate.evaluate_gate(conn, "c1").result == "NOT_PROVEN_SAFE"

        ledger.upsert_work_item(conn, "c1", "billing", "verified", "provider_patch")
        ledger.upsert_work_item(conn, "c1", "notifier", "verified", "subscribe")
        d = gate.evaluate_gate(conn, "c1")
        assert d.result == "NOT_PROVEN_SAFE"
        assert "notifier:webhook_quiet" in d.unresolved

        ledger.upsert_work_item(conn, "c1", "notifier", "verified", "webhook_quiet")
        d = gate.evaluate_gate(conn, "c1")
        assert d.result == "NOT_PROVEN_SAFE"
        assert "billing:coexistence_rehearsal" in d.unresolved

        # The provider must also have demonstrated that it holds both contracts
        # open at once. Nothing else in the run proves that property.
        ledger.upsert_work_item(
            conn, "c1", "billing", "verified", gate.REHEARSAL_STEP_KIND
        )
        assert gate.evaluate_gate(conn, "c1").result == "VERIFIED"

    def test_a_failed_provider_patch_blocks_the_gate(self, conn):
        """
        Consumers migrated to a field the provider never gained are not safe.
        This was a real bug: the gate reported VERIFIED with the patch failed.
        """
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", FIELD_RENAME_SPEC)
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        ledger.upsert_work_item(conn, "c1", "checkout", "verified", "migrate")

        d = gate.evaluate_gate(conn, "c1")
        assert d.result == "NOT_PROVEN_SAFE"
        assert "account-service:provider_patch" in d.unresolved

        ledger.upsert_work_item(conn, "c1", "account-service", "verified", "provider_patch")
        d = gate.evaluate_gate(conn, "c1")
        assert d.result == "NOT_PROVEN_SAFE"
        assert "account-service:coexistence_rehearsal" in d.unresolved

        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", gate.REHEARSAL_STEP_KIND
        )
        assert gate.evaluate_gate(conn, "c1").result == "VERIFIED"

    def test_a_failed_rehearsal_blocks_the_gate(self, conn):
        """
        The rehearsal proves the one property nothing else does: that a single
        running provider serves the old and new shapes at once.

        It used to write evidence and no work item, and the gate counts work
        items and never reads evidence. So a rehearsal that failed — or that
        never ran at all — left the verdict completely unchanged. Every
        consumer verified plus a broken coexistence window still read VERIFIED.
        """
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", FIELD_RENAME_SPEC)
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        ledger.upsert_work_item(conn, "c1", "checkout", "verified", "migrate")
        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", "provider_patch"
        )
        ledger.upsert_work_item(
            conn, "c1", "account-service", "failed", gate.REHEARSAL_STEP_KIND
        )

        d = gate.evaluate_gate(conn, "c1")
        assert d.result == "NOT_PROVEN_SAFE"
        assert "account-service:coexistence_rehearsal" in d.unresolved

    def test_a_missing_rehearsal_blocks_the_gate(self, conn):
        """Absence of proof is not proof: no rehearsal row must block too."""
        ledger.create_change(conn, "c1", "x")
        ledger.set_change_spec(conn, "c1", "field_rename", FIELD_RENAME_SPEC)
        ledger.add_dependency(conn, "c1", "account-service", "checkout", "api", None)
        ledger.upsert_work_item(conn, "c1", "checkout", "verified", "migrate")
        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", "provider_patch"
        )

        assert gate.evaluate_gate(conn, "c1").result == "NOT_PROVEN_SAFE"

    def test_single_step_kinds_keep_unqualified_names(self, conn):
        """Legacy output shape is preserved for single-step change kinds."""
        ledger.create_change(conn, "c1", "legacy")
        ledger.add_dependency(conn, "c1", gate.PROVIDER, "checkout", "api", None)
        assert gate.evaluate_gate(conn, "c1").unresolved == ["checkout"]


# ---------------------------------------------------------------------------
# Spec plumbing
# ---------------------------------------------------------------------------

class TestSymbolsFor:
    def test_field_kinds_use_field_names(self):
        assert symbols_for(FIELD_RENAME_SPEC) == ("customer_id", "account_id")

    def test_transport_kind_uses_symbols(self):
        spec = {"kind": "transport_migration", "old_symbol": "a", "new_symbol": "b"}
        assert symbols_for(spec) == ("a", "b")


# ---------------------------------------------------------------------------
# End to end — real agents, real commits, isolated workspace
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    """API client with an in-memory ledger and a throwaway workspace root."""
    monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
    with TestClient(main.app) as client:
        main.app.state.conn.close()
        main.app.state.conn = ledger.init_db(":memory:")
        yield client


@pytest.mark.integration
class TestEndToEnd:
    def test_real_agents_drive_a_change_to_verified(self, api):
        created = api.post(
            "/change-requests",
            json={"description": "customer_id -> account_id", "spec": FIELD_RENAME_SPEC},
        )
        assert created.status_code == 201
        change_id = created.json()["id"]
        assert created.json()["status"] == "COORDINATE"

        # The undocumented consumer must arrive from source inspection.
        graph = api.get(f"/change-requests/{change_id}/graph").json()
        edges = {(e["from"], e["to"]): e["edge_type"] for e in graph["edges"]}
        assert ("account-service", "analytics-worker") in edges
        assert edges[("account-service", "analytics-worker")] == "event"

        # No self-edge: a provider does not depend on itself.
        assert ("account-service", "account-service") not in edges

        api.post(
            f"/change-requests/{change_id}/approve",
            json={"gate": "coordinate", "approved_by": "tester"},
        )

        gate_status = api.get(f"/change-requests/{change_id}/gate").json()
        assert gate_status["result"] == "VERIFIED", gate_status["reason"]

    def test_commit_refs_are_real_distinct_shas(self, api):
        change_id = api.post(
            "/change-requests",
            json={"description": "x", "spec": FIELD_RENAME_SPEC},
        ).json()["id"]
        api.post(
            f"/change-requests/{change_id}/approve",
            json={"gate": "coordinate", "approved_by": "tester"},
        )

        evidence = api.get(f"/change-requests/{change_id}/evidence").json()["evidence"]
        shas = {
            e["source_revision"]
            for e in evidence
            if e["claim_type"] == "migration_status" and e["source_revision"]
        }
        assert len(shas) >= 3, "each component must produce its own commit"
        assert all(len(s) == 40 for s in shas), "SHAs must be real, not placeholders"

    def test_the_real_fixtures_are_never_mutated(self, api):
        """
        Implementation agents git-commit into the path they are given, and
        fixtures/ lives inside this repository. They must only ever see a copy.
        """
        fixtures = REPO_ROOT / "fixtures"
        before = {
            p.relative_to(fixtures): p.read_bytes()
            for p in fixtures.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }

        change_id = api.post(
            "/change-requests", json={"description": "x", "spec": FIELD_RENAME_SPEC}
        ).json()["id"]
        api.post(
            f"/change-requests/{change_id}/approve",
            json={"gate": "coordinate", "approved_by": "tester"},
        )

        after = {
            p.relative_to(fixtures): p.read_bytes()
            for p in fixtures.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert before == after

    def test_a_change_without_a_spec_still_uses_the_stub_path(self, api):
        """Back-compat: existing clients send no spec and must be unaffected."""
        created = api.post("/change-requests", json={"description": "customer_id -> account_id"})
        assert created.status_code == 201
        change_id = created.json()["id"]
        assert api.get(f"/change-requests/{change_id}/spec").json()["kind"] is None


class TestSpecEndpoint:
    def test_spec_is_returned_for_a_specced_change(self, api):
        change_id = api.post(
            "/change-requests",
            json={"description": "x", "spec": FIELD_RENAME_SPEC},
        ).json()["id"]
        body = api.get(f"/change-requests/{change_id}/spec").json()
        assert body["kind"] == "field_rename"
        assert body["spec"]["provider"] == "account-service"

    def test_unknown_change_is_404(self, api):
        assert api.get("/change-requests/nope/spec").status_code == 404

    def test_change_response_shape_is_unchanged(self, api):
        """Invariant 7: adding the spec must not alter existing responses."""
        body = api.post("/change-requests", json={"description": "x"}).json()
        assert set(body) == {
            "id", "description", "status", "entered_at",
            "retry_count", "created_at", "updated_at",
        }


class TestCors:
    def test_cors_headers_are_present(self, api):
        resp = api.get(
            "/change-requests/nope/spec", headers={"Origin": "http://localhost:8501"}
        )
        assert resp.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# Workspace path shape
# ---------------------------------------------------------------------------

class TestWorkspaceIsAbsolute:
    """
    Regression: `prepare_workspace` used to return the raw
    `workspace_root() / change_id`, which is RELATIVE under the default
    `.interlock_work`. Agents that decide whether to join a handed-over path
    against their own `repo_path` test `is_absolute()`, so a relative workspace
    produced `<workspace>/account-service/<workspace>/account-service`; the
    coexistence rehearsal never found the provider and every change came back
    NOT_PROVEN_SAFE.

    The whole suite stayed green because every test sets INTERLOCK_WORKSPACE to
    an absolute tmp_path — the bug only appeared with the default. These tests
    exercise the default explicitly.
    """

    def test_default_workspace_resolves_to_an_absolute_path(self, tmp_path, monkeypatch):
        from orchestrator.real_workflow import prepare_workspace

        monkeypatch.chdir(tmp_path)
        (tmp_path / "components" / "svc").mkdir(parents=True)
        monkeypatch.delenv("INTERLOCK_WORKSPACE", raising=False)

        workspace = prepare_workspace("change-1", "components")
        assert workspace.is_absolute(), "agents branch on is_absolute()"

    def test_explicit_relative_workspace_is_still_absolute(self, tmp_path, monkeypatch):
        from orchestrator.real_workflow import prepare_workspace

        monkeypatch.chdir(tmp_path)
        (tmp_path / "components" / "svc").mkdir(parents=True)
        monkeypatch.setenv("INTERLOCK_WORKSPACE", "relative_work")

        assert prepare_workspace("change-2", "components").is_absolute()

    def test_provider_path_handed_to_agents_is_absolute(self, tmp_path, monkeypatch):
        """
        The concrete contract: what the orchestrator puts in the agent context
        must not need joining against repo_path.
        """
        from orchestrator.real_workflow import _build_context, prepare_workspace
        from orchestrator.agent_registry import COEXISTENCE_REHEARSAL

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("INTERLOCK_WORKSPACE", raising=False)
        (tmp_path / "components" / "account-service").mkdir(parents=True)

        conn = ledger.init_db(":memory:")
        ledger.create_change(conn, "c1", "x")
        spec = {**FIELD_RENAME_SPEC, "components_root": "components"}
        ledger.set_change_spec(conn, "c1", "field_rename", spec)
        spec_row = ledger.get_change_spec(conn, "c1")

        workspace = prepare_workspace("c1", "components")
        ctx = _build_context(conn, "c1", spec_row, COEXISTENCE_REHEARSAL, workspace)

        assert Path(ctx["data"]["provider_path"]).is_absolute()
        assert Path(ctx["repo_path"]).is_absolute()
        conn.close()
