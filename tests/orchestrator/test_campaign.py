"""
tests/orchestrator/test_campaign.py
====================================
Tests for running several related changes as one unit.

The property this file exists to protect: **a campaign is not a second gate.**
`combine()` folds verdicts that `gate.evaluate_gate()` already produced, and it
can only ever be as strict as they are. If a campaign could report VERIFIED
while any change in it was not, the guarantee the whole product rests on would
have a second, weaker door.

The rest is ordering. A provider must be changed before anything that consumes
it, or the consumer migrates against a contract that is about to move. The
ordering is derived from discovered consumers rather than asked of a model, and
the one subtlety - two changes to the same symbol appear to block each other -
is the reason mutual edges are dropped rather than reported as a deadlock.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import campaign


def _change(name: str, provider: str, kind: str = "field_rename"):
    return campaign.PlannedChange(
        name=name,
        spec={
            "provider": provider, "kind": kind,
            "old_field": "customer_id", "new_field": "account_id",
        },
    )


# ---------------------------------------------------------------------------
# The combined verdict
# ---------------------------------------------------------------------------

class TestCombine:
    def test_all_verified_is_verified(self):
        result, reason = campaign.combine([
            {"name": "a", "result": "VERIFIED"},
            {"name": "b", "result": "VERIFIED"},
        ])
        assert result == "VERIFIED"
        assert "2 change(s)" in reason

    def test_one_unproven_sinks_the_campaign(self):
        """
        No partial credit, deliberately. The changes are related: one unproven
        change means nobody verified the state the estate would end up in.
        """
        result, reason = campaign.combine([
            {"name": "a", "result": "VERIFIED"},
            {"name": "b", "result": "NOT_PROVEN_SAFE"},
            {"name": "c", "result": "VERIFIED"},
        ])
        assert result == "NOT_PROVEN_SAFE"
        assert "b" in reason

    def test_an_empty_campaign_is_not_trivially_verified(self):
        """
        Nothing was checked, so nothing was proven. Returning VERIFIED for a
        plan that did nothing is the fabricated result invariant 4 forbids.
        """
        result, reason = campaign.combine([])
        assert result == "NOT_PROVEN_SAFE"
        assert "nothing was proven" in reason

    def test_a_change_that_could_not_run_is_not_a_pass(self):
        result, _ = campaign.combine([
            {"name": "a", "result": "VERIFIED"},
            {"name": "b", "result": "not_run"},
        ])
        assert result == "NOT_PROVEN_SAFE"

    @pytest.mark.parametrize("verdict", ["verified", "Verified", "OK", "", None])
    def test_only_the_exact_verdict_string_counts_as_passing(self, verdict):
        """
        Anything that is not exactly VERIFIED is not verified. A near-miss
        spelling must never be read as a pass.
        """
        result, _ = campaign.combine([{"name": "a", "result": verdict}])
        assert result == "NOT_PROVEN_SAFE"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_a_provider_runs_before_its_consumer(self):
        ordered, cycles = campaign.order_changes(
            [_change("b", "checkout"), _change("a", "account-service")],
            {"account-service": {"checkout"}, "checkout": set()},
        )
        assert [c.name for c in ordered] == ["a", "b"]
        assert cycles == []

    def test_a_symmetric_pair_is_not_a_cycle(self):
        """
        Two changes to the same symbol on different providers each appear among
        the other's consumers, because discovery reports every component that
        references a symbol without asserting which end owns it. That carries no
        ordering information, and treating it as a deadlock made perfectly
        runnable plans refuse to start.
        """
        ordered, cycles = campaign.order_changes(
            [_change("a", "svc-a"), _change("b", "svc-b")],
            {"svc-a": {"svc-b"}, "svc-b": {"svc-a"}},
        )
        assert [c.name for c in ordered] == ["a", "b"]
        assert cycles == []

    def test_a_real_cycle_is_reported_not_broken(self):
        """
        A → B → C → A with no mutual pair is a genuine contradiction in the
        plan. Silently linearising it would pick an order nobody chose.
        """
        ordered, cycles = campaign.order_changes(
            [_change("a", "x"), _change("b", "y"), _change("c", "z")],
            {"x": {"y"}, "y": {"z"}, "z": {"x"}},
        )
        assert ordered == []
        assert cycles == ["a", "b", "c"]

    def test_independent_changes_keep_the_order_written(self):
        """Stable: the same plan must always run the same way."""
        ordered, _ = campaign.order_changes(
            [_change("z", "one"), _change("y", "two"), _change("x", "three")],
            {"one": set(), "two": set(), "three": set()},
        )
        assert [c.name for c in ordered] == ["z", "y", "x"]


# ---------------------------------------------------------------------------
# Building a plan
# ---------------------------------------------------------------------------

class TestBuildPlan:
    def test_a_valid_plan_builds(self, monkeypatch):
        import pathlib

        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        planned, problems = campaign.build_plan([
            {"name": "one", "provider": "account-service",
             "old": "customer_id", "new": "account_id"},
        ], "fixtures")
        assert problems == []
        assert planned[0].provider == "account-service"

    def test_every_problem_is_reported_at_once(self, monkeypatch):
        """
        Fixing a plan one error per run is how people give up on a tool.
        """
        import pathlib

        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        _, problems = campaign.build_plan([
            {"name": "no-provider", "old": "a", "new": "b"},
            {"name": "bad-provider", "provider": "nope", "old": "a", "new": "b"},
            {"name": "bad-kind", "provider": "account-service",
             "old": "a", "new": "b", "kind": "nonsense"},
        ], "fixtures")
        assert len(problems) == 3

    def test_a_duplicate_name_is_refused(self, monkeypatch):
        import pathlib

        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        _, problems = campaign.build_plan([
            {"name": "same", "provider": "account-service", "old": "a", "new": "b"},
            {"name": "same", "provider": "account-service", "old": "c", "new": "d"},
        ], "fixtures")
        assert any("duplicate" in p for p in problems)

    def test_an_oversized_plan_is_refused_before_anything_runs(self, monkeypatch):
        """
        Each change copies the tree and runs real suites. A malformed plan must
        fail in a second, not after an hour.
        """
        import pathlib

        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        entries = [
            {"name": f"c{i}", "provider": "account-service", "old": "a", "new": "b"}
            for i in range(campaign.MAX_CHANGES + 1)
        ]
        planned, problems = campaign.build_plan(entries, "fixtures")
        assert planned == []
        assert any("capped" in p for p in problems)


class TestPlanFile:
    def test_yaml_with_a_changes_key(self, tmp_path):
        path = tmp_path / "plan.yaml"
        path.write_text(
            "changes:\n  - name: one\n    provider: svc\n    old: a\n    new: b\n",
            encoding="utf-8",
        )
        assert campaign.load_plan_file(str(path))[0]["name"] == "one"

    def test_a_bare_json_list(self, tmp_path):
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps([{"name": "one", "provider": "svc", "old": "a", "new": "b"}]),
            encoding="utf-8",
        )
        assert campaign.load_plan_file(str(path))[0]["provider"] == "svc"

    def test_a_mapping_without_a_changes_key_names_the_problem(self, tmp_path):
        """
        Defaulting to an empty list turned a typo'd key into "no runnable
        changes", which reads as a problem with the repository rather than with
        the file the author just wrote.
        """
        path = tmp_path / "plan.json"
        path.write_text(json.dumps({"chagnes": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="'changes' key"):
            campaign.load_plan_file(str(path))

    def test_a_plan_that_is_not_a_list_is_refused(self, tmp_path):
        path = tmp_path / "plan.json"
        path.write_text(json.dumps({"changes": 5}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            campaign.load_plan_file(str(path))


# ---------------------------------------------------------------------------
# The model's role
# ---------------------------------------------------------------------------

class TestTheModelOnlyChoosesWhatToCheck:
    def test_a_proposal_naming_an_unreal_provider_is_discarded(self):
        from orchestrator.watsonx import _parse_campaign

        reply = json.dumps([
            {"name": "fake", "kind": "field_rename", "provider": "does-not-exist",
             "old": "a", "new": "b", "reason": "invented"},
            {"name": "real", "kind": "field_rename", "provider": "checkout",
             "old": "a", "new": "b", "reason": "fine"},
        ])
        parsed = _parse_campaign(reply, {"checkout", "billing"})
        assert [c["provider"] for c in parsed] == ["checkout"]

    def test_an_unknown_kind_is_discarded(self):
        from orchestrator.watsonx import _parse_campaign

        reply = json.dumps([{
            "name": "x", "kind": "rewrite_everything", "provider": "checkout",
            "old": "a", "new": "b", "reason": "",
        }])
        assert _parse_campaign(reply, {"checkout"}) == []

    @pytest.mark.parametrize("reply", ["", "not json", "{}", "[1,2]", '[{"name":"x"}]'])
    def test_a_malformed_proposal_yields_nothing(self, reply):
        from orchestrator.watsonx import _parse_campaign

        assert _parse_campaign(reply, {"checkout"}) == []

    def test_planning_without_credentials_produces_no_changes(self, monkeypatch):
        """
        The plan-file route always works. A missing model means an empty plan,
        never a guessed one.
        """
        import pathlib

        from interlock_cli import core

        monkeypatch.delenv("INTERLOCK_ENABLE_NARRATION", raising=False)
        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        plan = core.campaign_plan("fixtures", None, "move everything off customer_id")
        assert plan["changes"] == []
        assert plan["runnable"] is False

    def test_the_system_prompt_denies_the_model_any_say_in_the_verdict(self):
        from orchestrator.watsonx import _CAMPAIGN_SYSTEM

        lowered = _CAMPAIGN_SYSTEM.lower()
        assert "not what passes" in lowered
        assert "never invent" in lowered


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

class TestRunning:
    @pytest.fixture
    def conn(self, tmp_path, monkeypatch):
        import pathlib

        from interlock_cli import core

        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "w"))
        monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[2])
        return core.open_ledger(str(tmp_path / "l.db"))

    def test_later_changes_are_not_run_after_a_failure(self, conn, monkeypatch):
        """
        The changes are ordered by dependency, so once one fails the ones after
        it would be migrating against a contract that never moved. Reporting
        them as `not_run` is honest; giving them a verdict is not.
        """
        from orchestrator import campaign as mod

        calls = []

        def _fake_check(conn, description, spec):
            calls.append(spec["provider"])
            return {
                "change_id": f"id-{len(calls)}",
                "gate": {"result": "NOT_PROVEN_SAFE", "reason": "planted",
                         "unresolved": ["x"]},
            }

        from interlock_cli import core

        monkeypatch.setattr(core, "check", _fake_check)
        result = mod.run_campaign(
            conn, "test", [_change("a", "account-service"), _change("b", "checkout")]
        )
        assert len(calls) == 1
        assert result.changes[1]["result"] == "not_run"
        assert result.result == "NOT_PROVEN_SAFE"

    def test_keep_going_attempts_them_all(self, conn, monkeypatch):
        from interlock_cli import core
        from orchestrator import campaign as mod

        calls = []

        def _fake_check(conn, description, spec):
            calls.append(spec["provider"])
            return {"change_id": "x", "gate": {"result": "NOT_PROVEN_SAFE",
                                               "reason": "planted", "unresolved": []}}

        monkeypatch.setattr(core, "check", _fake_check)
        mod.run_campaign(
            conn, "test", [_change("a", "account-service"), _change("b", "checkout")],
            stop_on_failure=False,
        )
        assert len(calls) == 2

    def test_a_crashing_change_is_recorded_as_failed_not_skipped(self, conn, monkeypatch):
        from interlock_cli import core
        from orchestrator import campaign as mod

        def _boom(conn, description, spec):
            raise RuntimeError("workspace exploded")

        monkeypatch.setattr(core, "check", _boom)
        result = mod.run_campaign(conn, "test", [_change("a", "account-service")])
        assert result.result == "NOT_PROVEN_SAFE"
        assert "workspace exploded" in result.changes[0]["reason"]
