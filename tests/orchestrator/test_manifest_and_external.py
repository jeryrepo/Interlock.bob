"""
tests/orchestrator/test_manifest_and_external.py
=================================================
Tests for the two things that make Interlock work beyond Python field renames:
the component manifest, and the external implementation mode.

Together they are what makes a C-to-Python port expressible. Interlock does not
translate the code — a human or another coding agent does — and Interlock proves
the symbols moved, every component's own suite passes, and the old path is not
retired until they do.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import orchestrator.ledger as ledger
from orchestrator.agent_registry import agents_for
from orchestrator.manifest import MANIFEST_FILENAME, load

REPO_ROOT = Path(__file__).resolve().parents[2]
POLYGLOT = REPO_ROOT / "fixtures_polyglot"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_absent_manifest_falls_back_to_pytest(self, tmp_path):
        """Existing Python components need no manifest."""
        m = load(tmp_path)
        assert m.declared is False
        assert m.uses_default_pytest
        assert "pytest" in " ".join(m.command())

    def test_declared_command_is_used(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text(
            '[component]\nlanguage = "c"\ntest_command = "make test"\n', encoding="utf-8"
        )
        m = load(tmp_path)
        assert m.language == "c"
        assert m.command() == ["make", "test"]
        assert not m.uses_default_pytest

    def test_argv_list_form_is_accepted(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text(
            '[component]\ntest_command = ["go", "test", "./..."]\n', encoding="utf-8"
        )
        assert load(tmp_path).command() == ["go", "test", "./..."]

    def test_malformed_manifest_falls_back_rather_than_raising(self, tmp_path):
        """
        A typo in one component's config must not abort a whole change. The
        right place to fail loudly is the test run, where it is attributable.
        """
        (tmp_path / MANIFEST_FILENAME).write_text("this is not toml {{{", encoding="utf-8")
        assert load(tmp_path).uses_default_pytest

    def test_coexistence_command_is_read(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text(
            '[component]\ncoexistence_command = "sh check.sh"\n', encoding="utf-8"
        )
        assert load(tmp_path).coexistence_command == ["sh", "check.sh"]

    def test_command_is_never_shell_interpreted(self, tmp_path):
        """
        A manifest is data from the repository under test. Splitting rather than
        shelling out means it cannot chain a second command.
        """
        (tmp_path / MANIFEST_FILENAME).write_text(
            '[component]\ntest_command = "echo hi; rm -rf /"\n', encoding="utf-8"
        )
        command = load(tmp_path).command()
        assert command[0] == "echo"
        assert ";" in command or "rm" in command  # present as literal argv, not a chain


# ---------------------------------------------------------------------------
# Language-agnostic verification
# ---------------------------------------------------------------------------

class TestNonPythonVerification:
    """`python -m pytest` was the one line confining Interlock to Python."""

    def _component(self, tmp_path: Path, *, passing: bool) -> Path:
        root = tmp_path / "c-lib"
        root.mkdir()
        (root / MANIFEST_FILENAME).write_text(
            '[component]\nlanguage = "c"\ntest_command = "sh run_tests.sh"\n',
            encoding="utf-8",
        )
        (root / "lib.c").write_text(
            "int calc_v2(int x) { return x; }\n" if passing else "int nothing(void){return 0;}\n",
            encoding="utf-8",
        )
        (root / "run_tests.sh").write_text(
            '#!/bin/sh\ngrep -q "calc_v2" lib.c || exit 1\necho "1 passed"\n',
            encoding="utf-8",
        )
        return root

    def test_a_c_component_can_pass(self, tmp_path):
        from agents.verification import contract_test

        result = contract_test.run({"change_id": "c1"}, self._component(tmp_path, passing=True))
        content = result["evidence"][0]["content"]
        assert result["status"] == "verified"
        assert content["language"] == "c"
        assert content["test_command"] == ["sh", "run_tests.sh"]

    def test_a_c_component_can_fail(self, tmp_path):
        from agents.verification import contract_test

        result = contract_test.run({"change_id": "c1"}, self._component(tmp_path, passing=False))
        assert result["status"] == "failed"
        assert result["evidence"][0]["content"]["outcome"] == "tests_failed"

    def test_an_unrunnable_command_is_not_a_pass(self, tmp_path):
        from agents.verification import contract_test

        root = tmp_path / "broken"
        root.mkdir()
        (root / MANIFEST_FILENAME).write_text(
            '[component]\ntest_command = "definitely-not-a-real-binary"\n', encoding="utf-8"
        )
        result = contract_test.run({"change_id": "c1"}, root)
        assert result["status"] == "failed"
        assert result["evidence"][0]["content"]["outcome"] == "tests_could_not_run"


# ---------------------------------------------------------------------------
# External mode
# ---------------------------------------------------------------------------

class TestExternalRegistry:
    def test_external_swaps_only_the_modify_phase(self):
        """Only the means of changing code differs; the proof required does not."""
        builtin = [a.role for a in agents_for("field_rename", "MODIFY")]
        external = [a.role for a in agents_for("field_rename", "MODIFY", "external")]
        assert builtin != external
        assert all("external" in r for r in external)

        for phase in ("DISCOVERY", "PLANNING", "REHEARSE", "VERIFY"):
            assert agents_for("field_rename", phase) == agents_for(
                "field_rename", phase, "external"
            )

    def test_unknown_kind_still_gets_external_agents(self):
        """A new transition type is usable before anyone writes a rewriter."""
        assert agents_for("some_future_transition", "MODIFY", "external")

    def test_external_agents_preserve_the_step_kinds_the_gate_counts(self):
        steps = {a.step_kind for a in agents_for("transport_migration", "MODIFY", "external")}
        assert steps == {"provider_patch", "subscribe"}


class TestExternalChangeAgent:
    """It verifies work someone else did. It must never edit, nor over-report."""

    def _repo(self, tmp_path: Path, body: str) -> Path:
        root = tmp_path / "svc"
        root.mkdir()
        (root / "code.py").write_text(body, encoding="utf-8")
        # Use a cross-platform test command — "sh" is unavailable on Windows.
        (root / MANIFEST_FILENAME).write_text(
            '[component]\ntest_command = "python -c \'import sys; sys.exit(0)\'"\n',
            encoding="utf-8",
        )
        for args in (["init"], ["config", "user.email", "t@e.com"],
                     ["config", "user.name", "t"], ["add", "."], ["commit", "-m", "x"]):
            subprocess.run(["git", "-C", str(root), *args], capture_output=True)
        return root

    def test_a_migrated_consumer_is_success(self, tmp_path):
        from agents.implementation import external_change

        root = self._repo(tmp_path, "value = calc_py(1)\n")
        out = external_change.run(
            {"change_id": "c1", "old_field": "calc_legacy_c", "new_field": "calc_py"}, root
        )
        assert out["status"] == "success"
        assert len(out["commit_sha"]) == 40

    def test_a_consumer_still_using_the_old_symbol_fails(self, tmp_path):
        from agents.implementation import external_change

        root = self._repo(tmp_path, "value = calc_legacy_c(1)\n")
        out = external_change.run(
            {"change_id": "c1", "old_field": "calc_legacy_c", "new_field": "calc_py"}, root
        )
        assert out["status"] == "failed"
        assert "calc_legacy_c" in out["summary"]

    def test_a_provider_must_keep_both_symbols(self, tmp_path):
        """Dropping the old path mid-window breaks every un-migrated consumer."""
        from agents.implementation import external_change

        root = self._repo(tmp_path, "def calc_py(x): return x\n")
        out = external_change.run(
            {"change_id": "c1", "old_field": "calc_legacy_c", "new_field": "calc_py",
             "consumer": "svc", "provider": "svc"},
            root,
        )
        assert out["status"] == "failed"
        assert "no longer references" in out["summary"]

    def test_it_never_modifies_the_component(self, tmp_path):
        from agents.implementation import external_change

        root = self._repo(tmp_path, "value = calc_py(1)\n")
        before = {p.name: p.read_bytes() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.parts}
        external_change.run(
            {"change_id": "c1", "old_field": "calc_legacy_c", "new_field": "calc_py"}, root
        )
        after = {p.name: p.read_bytes() for p in root.rglob("*")
                 if p.is_file() and ".git" not in p.parts}
        assert before == after
        assert external_change.run(
            {"change_id": "c1", "old_field": "calc_legacy_c", "new_field": "calc_py"}, root
        )["files_changed"] == []


# ---------------------------------------------------------------------------
# Failure must not be undone by a later phase
# ---------------------------------------------------------------------------

class TestFailureIsSticky:
    def test_verification_cannot_clear_a_failed_implementation(self, tmp_path, monkeypatch):
        """
        Regression: a consumer that was never migrated still had a green suite
        of its own, so the VERIFY-phase contract test overwrote the MODIFY-phase
        failure and the gate reported VERIFIED. Verification confirms a step; it
        must never overturn a failure.
        """
        from orchestrator.real_workflow import _already_failed

        conn = ledger.init_db(":memory:")
        ledger.create_change(conn, "c1", "x")
        ledger.upsert_work_item(conn, "c1", "reporting", "failed", "migrate")

        assert _already_failed(conn, "c1", "reporting", "migrate") is True
        assert _already_failed(conn, "c1", "reporting", "subscribe") is False
        assert _already_failed(conn, "c1", "billing", "migrate") is False
        conn.close()


# ---------------------------------------------------------------------------
# The polyglot fixture tree, end to end
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPolyglotMigration:
    """
    A C-to-Python port across three toolchains: a C provider, a shell-tested
    consumer, and a Python consumer with no manifest at all.
    """

    ARGS = [
        "check", "--implementation", "external",
        "--old", "calc_legacy_c", "--new", "calc_py",
        "--provider", "calc-core", "--components-root", "fixtures_polyglot",
    ]

    @pytest.fixture
    def db(self, tmp_path, monkeypatch) -> str:
        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
        monkeypatch.chdir(REPO_ROOT)
        return str(tmp_path / "ledger.db")

    def test_reaches_verified_across_three_toolchains(self, db):
        from typer.testing import CliRunner

        from interlock_cli import core
        from interlock_cli.cli import app

        result = CliRunner().invoke(app, [*self.ARGS, "--db", db, "--json"])
        assert result.exit_code == core.EXIT_OK, result.stdout
        payload = json.loads(result.stdout)
        assert payload["gate"]["result"] == "VERIFIED"

        components = {w["component"] for w in payload["gate"]["work_items"]}
        assert {"calc-core", "billing", "reporting"} <= components

    def test_the_c_provider_proves_coexistence_its_own_way(self, db):
        """It is a library, not a web service; the HTTP probe would be nonsense."""
        from typer.testing import CliRunner

        from interlock_cli.cli import app

        result = CliRunner().invoke(app, [*self.ARGS, "--db", db, "--json"])
        items = {
            (w["component"], w["step_kind"]): w["status"]
            for w in json.loads(result.stdout)["gate"]["work_items"]
        }
        assert items[("calc-core", "coexistence_rehearsal")] == "verified"

    def test_polyglot_fixtures_are_never_mutated(self, db):
        from typer.testing import CliRunner

        from interlock_cli.cli import app

        before = {
            p.relative_to(POLYGLOT): p.read_bytes()
            for p in POLYGLOT.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        CliRunner().invoke(app, [*self.ARGS, "--db", db])
        after = {
            p.relative_to(POLYGLOT): p.read_bytes()
            for p in POLYGLOT.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert before == after
