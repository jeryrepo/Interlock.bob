"""
tests/cli/test_review_and_kinds.py
===================================
End-to-end coverage for all three change kinds, plus the PR review renderer
and the orchestration map.

The review renderer is tested as a pure function because that is the whole
reason it was moved out of the workflow's inline JavaScript: a formatter that
only runs on a real pull request is a formatter you debug in production.

The `integration`-marked cases run real agents against real fixture trees and
make real git commits inside a throwaway workspace. No network, no Docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import orchestrator.ledger as ledger
from interlock_cli import core, review
from interlock_cli.cli import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.chdir(REPO_ROOT)
    return tmp_path


@pytest.fixture
def db(env) -> str:
    return str(env / "ledger.db")


# ---------------------------------------------------------------------------
# The review renderer
# ---------------------------------------------------------------------------

def _status(result: str, unresolved=None, items=None, decided=True) -> dict:
    return {
        "change_id": "c-123",
        "description": "customer_id -> account_id",
        "state": "APPROVE",
        "kind": "field_rename",
        "gate": {
            "change_id": "c-123",
            "decided": decided,
            "result": result,
            "reason": "because reasons",
            "required_consumers": ["checkout"],
            "unresolved": unresolved or [],
            "work_items": items or [],
        },
    }


class TestReviewRenderer:
    def test_verdict_is_in_the_first_line(self):
        md = review.render_markdown(_status("VERIFIED"))
        assert md.splitlines()[0].startswith("## ")
        assert "VERIFIED" in md.splitlines()[0]

    def test_failure_is_unambiguous(self):
        md = review.render_markdown(_status("NOT_PROVEN_SAFE"))
        assert "NOT PROVEN SAFE" in md.splitlines()[0]

    def test_blocking_components_are_named(self):
        md = review.render_markdown(
            _status("NOT_PROVEN_SAFE", unresolved=["checkout", "notifier:webhook_quiet"])
        )
        assert "`checkout`" in md
        assert "`notifier:webhook_quiet`" in md

    def test_work_items_render_as_a_table(self):
        md = review.render_markdown(_status("VERIFIED", items=[
            {"component": "checkout", "step_kind": "migrate", "status": "verified"},
        ]))
        assert "| Component | Step | Status |" in md
        assert "`checkout`" in md and "migrated" in md

    def test_undocumented_consumers_get_their_own_section(self):
        """The hidden dependency is the whole point; it must not be buried."""
        graph = {"edges": [
            {"from": "account-service", "to": "checkout", "edge_type": "api"},
            {"from": "account-service", "to": "analytics-worker",
             "edge_type": "undocumented", "reason": "found in source"},
        ]}
        md = review.render_markdown(_status("VERIFIED"), graph)
        assert "not in any published contract" in md
        assert "analytics-worker" in md

    def test_documented_consumers_are_not_listed_as_hidden(self):
        graph = {"edges": [
            {"from": "account-service", "to": "checkout", "edge_type": "api"},
        ]}
        md = review.render_markdown(_status("VERIFIED"), graph)
        assert "not in any published contract" not in md

    def test_risks_are_surfaced(self):
        risks = [{"subject": "critic", "content": {
            "risk": "critic_not_run", "detail": "INTERLOCK_API_URL unset"}}]
        md = review.render_markdown(_status("VERIFIED"), None, risks)
        assert "critic_not_run" in md

    def test_undecided_gate_is_labelled_a_preview(self):
        """A preview must never read as a settled verdict."""
        md = review.render_markdown(_status("VERIFIED", decided=False))
        assert "preview" in md.lower()

    def test_footer_states_the_gate_cannot_be_overridden(self):
        md = review.render_markdown(_status("VERIFIED"))
        assert "no agent can override" in md

    def test_summary_is_one_line(self):
        s = review.render_summary(_status("NOT_PROVEN_SAFE", unresolved=["checkout"]))
        assert "\n" not in s
        assert "checkout" in s


# ---------------------------------------------------------------------------
# Orchestration map
# ---------------------------------------------------------------------------

class TestOrchestrationMap:
    def test_every_kind_appears(self, db):
        result = runner.invoke(app, ["agents", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert set(payload) == {
            "field_rename", "api_contract_change", "transport_migration"
        }

    def test_transport_requires_two_consumer_steps(self, db):
        payload = json.loads(runner.invoke(app, ["agents", "--json"]).stdout)
        req = payload["transport_migration"]["gate_requires"]
        assert req["per_consumer"] == ["subscribe", "webhook_quiet"]
        assert req["provider"] == ["provider_patch", "coexistence_rehearsal"]

    def test_every_required_step_has_an_agent_that_proves_it(self):
        """
        A step nothing proves would hold every change of that kind at
        NOT_PROVEN_SAFE forever. This test is what stops that shipping silently.
        """
        payload = json.loads(runner.invoke(app, ["agents", "--json"]).stdout)
        for kind, info in payload.items():
            proven = {
                a["proves"]
                for phase in info["phases"].values()
                for a in phase
                if a["proves"]
            }
            required = set(info["gate_requires"]["per_consumer"]) | set(
                info["gate_requires"]["provider"]
            )
            assert required <= proven, (
                f"{kind}: no agent proves {required - proven}"
            )


# ---------------------------------------------------------------------------
# All three change kinds, end to end
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFieldRename:
    def test_reaches_verified(self, db):
        result = runner.invoke(app, [
            "check", "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--db", db, "--json",
        ])
        assert result.exit_code == core.EXIT_OK, result.stdout
        assert json.loads(result.stdout)["gate"]["result"] == "VERIFIED"


@pytest.mark.integration
class TestApiContractChange:
    def test_reaches_verified(self, db):
        result = runner.invoke(app, [
            "check", "--kind", "api_contract_change",
            "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--db", db, "--json",
        ])
        assert result.exit_code == core.EXIT_OK, result.stdout
        payload = json.loads(result.stdout)
        assert payload["kind"] == "api_contract_change"
        assert payload["gate"]["result"] == "VERIFIED"


@pytest.mark.integration
class TestTransportMigration:
    ARGS = [
        "check", "--kind", "transport_migration",
        "--old", "deliver_via_webhook", "--new", "deliver_via_pubsub",
        "--provider", "event-publisher",
        "--components-root", "fixtures_transport",
    ]

    def test_blocks_because_the_provider_side_is_not_automatable(self, db):
        """
        A transport cut-over must NOT reach VERIFIED today, and this test exists
        to keep it that way until the provider side genuinely works.

        This previously asserted VERIFIED. It passed for the wrong reason: the
        provider-patch agent matches field-shaped symbols (class annotations,
        dict keys, assignments), and `deliver_via_webhook` is a function name,
        so none of its patterns matched. It changed nothing, committed nothing
        of substance, and reported success anyway — leaving `event-publisher`
        without `deliver_via_pubsub` while the gate said the migration was safe.

        Synthesising a real pub/sub implementation is beyond a deterministic
        agent, so the honest outcome is NOT_PROVEN_SAFE naming the provider
        steps that were never proved. Subscribers still migrate correctly; it is
        only the provider that cannot be automated.
        """
        result = runner.invoke(app, [*self.ARGS, "--db", db, "--json"])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE, result.stdout

        gate_payload = json.loads(result.stdout)["gate"]
        assert gate_payload["result"] == "NOT_PROVEN_SAFE"
        assert "event-publisher:provider_patch" in gate_payload["unresolved"]

    def test_both_steps_are_proved_for_every_subscriber(self, db):
        result = runner.invoke(app, [*self.ARGS, "--db", db, "--json"])
        items = json.loads(result.stdout)["gate"]["work_items"]
        by_component: dict[str, set[str]] = {}
        for w in items:
            if w["step_kind"] in ("subscribe", "webhook_quiet"):
                by_component.setdefault(w["component"], set()).add(w["step_kind"])
        assert by_component, "no subscriber work items"
        for component, steps in by_component.items():
            assert steps == {"subscribe", "webhook_quiet"}, component

    def test_the_undocumented_subscriber_is_discovered(self, db):
        """audit-sink is linked to the publisher only by source, never config."""
        result = runner.invoke(app, [*self.ARGS, "--db", db, "--json"])
        components = {w["component"] for w in json.loads(result.stdout)["gate"]["work_items"]}
        assert "audit-sink" in components

    def test_an_undrained_subscriber_blocks_the_gate(self, db, tmp_path, monkeypatch):
        """
        A subscriber that moved to pub/sub but still sends webhook traffic is
        NOT safe: retiring the webhook would still break it.
        """
        import shutil

        root = tmp_path / "transport"
        shutil.copytree(REPO_ROOT / "fixtures_transport", root)
        activity = root / "notifier" / "webhook_activity.json"
        data = json.loads(activity.read_text())
        data["calls_in_window"] = 5
        activity.write_text(json.dumps(data))

        result = runner.invoke(app, [
            "check", "--kind", "transport_migration",
            "--old", "deliver_via_webhook", "--new", "deliver_via_pubsub",
            "--provider", "event-publisher",
            "--components-root", str(root),
            "--db", db, "--json",
        ])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE
        payload = json.loads(result.stdout)
        assert "notifier:webhook_quiet" in payload["gate"]["unresolved"]

    def test_fixtures_transport_is_never_mutated(self, db):
        source = REPO_ROOT / "fixtures_transport"
        before = {
            p.relative_to(source): p.read_bytes()
            for p in source.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        runner.invoke(app, [*self.ARGS, "--db", db])
        after = {
            p.relative_to(source): p.read_bytes()
            for p in source.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert before == after


# ---------------------------------------------------------------------------
# The review command, against real runs
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestReviewCommand:
    def test_run_renders_markdown_and_exits_zero_when_verified(self, db):
        result = runner.invoke(app, [
            "review", "--run", "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--db", db,
        ])
        assert result.exit_code == core.EXIT_OK, result.stdout
        assert result.stdout.startswith("## ")
        assert "VERIFIED" in result.stdout

    def test_review_names_the_undocumented_consumer(self, db):
        result = runner.invoke(app, [
            "review", "--run", "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--db", db,
        ])
        assert "analytics-worker" in result.stdout
        assert "not in any published contract" in result.stdout

    def test_summary_format_is_one_line(self, db):
        result = runner.invoke(app, [
            "review", "--run", "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--db", db, "--format", "summary",
        ])
        assert len(result.stdout.strip().splitlines()) == 1

    def test_review_without_id_or_run_is_an_error(self, db):
        result = runner.invoke(app, ["review", "--db", db])
        assert result.exit_code == core.EXIT_ERROR

    def test_review_of_unknown_change_is_an_error(self, db):
        result = runner.invoke(app, ["review", "no-such-change", "--db", db])
        assert result.exit_code == core.EXIT_ERROR
