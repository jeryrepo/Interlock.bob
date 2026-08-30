"""
tests/cli/test_onboarding.py
=============================
Tests for first contact with an unfamiliar repository.

Everything Interlock does well is downstream of one structural assumption: that
`--components-root` points at a directory whose immediate subdirectories are the
components. Against the bundled fixtures that assumption is always true, so the
whole class of failures it causes was invisible until the tool was pointed at
something else.

Each test here corresponds to a way that first run went wrong:

- zero discovered consumers raised `InvalidTransition` and printed a traceback
- a `--provider` that is not a component ran the full workflow and then blamed
  the provider patch, or - worse, with a path-shaped value - classified the
  provider as a consumer and failed it for referencing its own symbol
- `interlock doctor` resolved the MCP server against the *working directory*, so
  it reported MISS and exited non-zero from every directory except this one
- every subdirectory counted as a component, including `.git`, `.venv` and
  `node_modules`, which made a real repository slow to scan and manufactured
  consumers the gate then demanded be migrated
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interlock_cli import core
from interlock_cli.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def at_repo_root(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture
def lonely(tmp_path):
    """
    A components root where the symbol appears only in the provider.

    The realistic empty-graph case: the change is real, but nothing outside the
    provider depends on it, so discovery legitimately finds no edges.
    """
    (tmp_path / "svc-a").mkdir()
    (tmp_path / "svc-b").mkdir()
    (tmp_path / "svc-a" / "service.py").write_text(
        "class A:\n    lonely_field: str = 'x'\n", encoding="utf-8"
    )
    (tmp_path / "svc-b" / "service.py").write_text(
        "def unrelated():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# What counts as a component
# ---------------------------------------------------------------------------

class TestComponentEnumeration:
    def test_build_output_and_vcs_are_not_components(self, tmp_path):
        """
        `node_modules` is not a service that breaks in production.

        These were enumerated as components, which cost two things: every
        scanner walked and read them, and each became a candidate consumer that
        the gate would require to be migrated - with no way to exclude it,
        because there is no ignore file.
        """
        for name in ("real-svc", "node_modules", ".git", ".venv", "dist", "build"):
            (tmp_path / name).mkdir()

        names = [c["name"] for c in core.list_components(str(tmp_path))]
        assert names == ["real-svc"]

    def test_dotted_directories_are_not_components(self, tmp_path):
        """`.github` holding a workflow that mentions the symbol is not a consumer."""
        (tmp_path / "svc").mkdir()
        (tmp_path / ".github").mkdir()
        assert [c["name"] for c in core.list_components(str(tmp_path))] == ["svc"]

    def test_every_scanner_agrees_about_this(self, tmp_path):
        """
        One definition, not five.

        The scanners each had their own copy of the enumeration loop, and they
        disagreed - only polyglot_source filtered. A component visible to one
        scanner and invisible to another produces a dependency graph that
        depends on which agent happened to run.
        """
        from agents.discovery.repo_map import component_dirs

        (tmp_path / "svc").mkdir()
        (tmp_path / "node_modules").mkdir()
        assert [d.name for d in component_dirs(tmp_path)] == ["svc"]
        assert component_dirs(tmp_path, exclude="svc") == []

    def test_manifest_presence_is_reported(self, tmp_path):
        """A component with no interlock.toml gets `python -m pytest .`."""
        (tmp_path / "go-svc").mkdir()
        (tmp_path / "py-svc").mkdir()
        (tmp_path / "go-svc" / "interlock.toml").write_text(
            '[component]\nlanguage = "go"\ntest_command = "go test ./..."\n',
            encoding="utf-8",
        )
        found = {c["name"]: c["has_manifest"] for c in core.list_components(str(tmp_path))}
        assert found == {"go-svc": True, "py-svc": False}

    def test_a_missing_root_is_empty_not_an_error(self, tmp_path):
        assert core.list_components(str(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# --provider
# ---------------------------------------------------------------------------

class TestProviderValidation:
    def test_a_real_component_has_no_problem(self, at_repo_root):
        assert core.provider_problem("account-service", "fixtures") is None

    def test_an_unknown_provider_lists_what_is_there(self, at_repo_root):
        problem = core.provider_problem("no-such-service", "fixtures")
        assert problem is not None
        assert "account-service" in problem

    def test_a_path_shaped_provider_says_so(self, at_repo_root):
        """
        The most confusing failure of all, caught up front.

        `services/api` resolves as a path, so the agents run - but the
        component's own name is `api`, so the provider is classified as a
        *consumer* and fails for still referencing the old symbol. Nothing in
        that output points at the argument that caused it.
        """
        problem = core.provider_problem("services/api", "fixtures")
        assert problem is not None
        assert "bare directory name" in problem

    def test_a_missing_root_is_reported_as_such(self, tmp_path):
        problem = core.provider_problem("svc", str(tmp_path / "nope"))
        assert problem is not None
        assert "does not exist" in problem

    def test_a_root_with_no_components_says_what_a_component_is(self, tmp_path):
        (tmp_path / "readme.md").write_text("x", encoding="utf-8")
        problem = core.provider_problem("svc", str(tmp_path))
        assert problem is not None
        assert "immediate subdirectory" in problem

    def test_the_cli_refuses_before_running_anything(self, at_repo_root, tmp_path):
        result = runner.invoke(app, [
            "check", "--old", "customer_id", "--new", "account_id",
            "--provider", "no-such-service", "--db", str(tmp_path / "l.db"),
        ])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE
        assert "account-service" in result.stdout
        # Refused up front: no workspace was ever built for it.
        assert not (tmp_path / "work").exists()


# ---------------------------------------------------------------------------
# An empty dependency graph
# ---------------------------------------------------------------------------

class TestNothingDiscovered:
    def test_check_explains_instead_of_crashing(self, lonely, tmp_path, monkeypatch):
        """
        The single most likely first run against an unfamiliar repository.

        `state_machine.can_advance` refuses to leave DISCOVERY with no edges,
        and nothing caught the resulting `InvalidTransition`, so this printed a
        traceback. The verdict was never wrong - it was unreadable.
        """
        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
        result = runner.invoke(app, [
            "check", "--old", "lonely_field", "--new", "new_field",
            "--provider", "svc-a", "--components-root", str(lonely),
            "--db", str(tmp_path / "l.db"),
        ])
        assert result.exit_code == core.EXIT_NOT_PROVEN_SAFE
        assert "Traceback" not in result.stdout
        assert "lonely_field" in result.stdout
        assert "svc-a, svc-b" in result.stdout

    def test_the_exception_carries_what_the_message_needs(self, lonely, tmp_path, monkeypatch):
        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
        conn = core.open_ledger(str(tmp_path / "l.db"))
        spec = core.build_spec(
            "field_rename", "svc-a", "lonely_field", "new_field", str(lonely)
        )
        with pytest.raises(core.NothingDiscovered) as caught:
            core.start(conn, "lonely", spec)
        assert caught.value.symbol == "lonely_field"
        assert caught.value.provider == "svc-a"

    def test_a_real_invalid_transition_still_propagates(self, at_repo_root, tmp_path):
        """
        Only the empty-graph case is translated.

        Swallowing every `InvalidTransition` would hide genuine state-machine
        bugs behind a friendly message about components, which is a worse
        failure than the traceback this replaced.
        """
        import orchestrator.state_machine as sm

        conn = core.open_ledger(str(tmp_path / "l.db"))
        spec = core.build_spec(
            "field_rename", "account-service", "customer_id", "account_id", "fixtures"
        )
        assert issubclass(sm.InvalidTransition, Exception)
        assert not issubclass(core.NothingDiscovered, sm.InvalidTransition)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

class TestDiscover:
    def test_it_finds_the_consumers(self, at_repo_root):
        result = core.discover("fixtures", "account-service", "customer_id", "account_id")
        assert [c["name"] for c in result["consumers"]] == [
            "analytics-worker", "checkout", "fraud", "platform-config",
        ]

    def test_it_flags_the_consumers_no_contract_mentions(self, at_repo_root):
        """
        The ones that break production.

        Not "has an undocumented edge" - every consumer picks one of those up
        the moment repo-map reads the symbol in its source, so that says
        nothing. What matters is the absence of an API contract: these couple
        through an event subscription or a shared table, so no contract review
        would ever have surfaced them.
        """
        result = core.discover("fixtures", "account-service", "customer_id", "account_id")
        hidden = {c["name"] for c in result["undocumented"]}
        assert hidden == {"analytics-worker", "platform-config"}

    def test_it_writes_nothing(self, at_repo_root, tmp_path, monkeypatch):
        """Read-only: no workspace, no git, no ledger, no state machine."""
        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
        core.discover("fixtures", "account-service", "customer_id", "account_id")
        assert not (tmp_path / "work").exists()
        assert list(tmp_path.iterdir()) == []

    def test_it_runs_every_discovery_agent(self, at_repo_root):
        result = core.discover("fixtures", "account-service", "customer_id", "account_id")
        assert result["agents_failed"] == []
        # The set, not a count: when this changes it should say which agent
        # appeared or vanished, not just that the number moved.
        assert set(result["agents_run"]) == {
            "repo-map",
            "api-contract-discovery",
            "event-contract-discovery",
            "db-schema-discovery",
            "polyglot-source-discovery",
            # Runs but returns nothing without IBM credentials, which is the
            # designed default: the deterministic scanners stand alone.
            "llm-discovery",
        }

    def test_an_empty_graph_is_reported_not_crashed(self, lonely):
        result = core.discover(str(lonely), "svc-a", "lonely_field")
        assert result["edges"] == []
        assert [c["name"] for c in result["components"]] == ["svc-a", "svc-b"]

    def test_it_names_a_provider_that_is_not_a_component(self, at_repo_root):
        result = core.discover("fixtures", "not-here", "customer_id")
        assert result["provider_is_a_component"] is False

    def test_the_cli_prints_a_verdict_free_report(self, at_repo_root):
        result = runner.invoke(app, [
            "discover", "--old", "customer_id", "--new", "account_id",
            "--provider", "account-service", "--components-root", "fixtures",
        ])
        assert result.exit_code == 0
        assert "5 component(s)" in result.stdout
        assert "analytics-worker" in result.stdout
        # discover reports; it never decides.
        assert "VERIFIED" not in result.stdout


# ---------------------------------------------------------------------------
# Finding the right components root
# ---------------------------------------------------------------------------

class TestSuggestRoots:
    @pytest.fixture
    def nested(self, tmp_path):
        """A repo that keeps its services one level down, as most do."""
        for name in ("orders", "billing", "shipping"):
            service = tmp_path / "services" / name
            service.mkdir(parents=True)
            (service / "main.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("no code\n", encoding="utf-8")
        return tmp_path

    def test_it_finds_the_level_that_holds_the_services(self, nested):
        found = core.suggest_roots(str(nested))
        assert str(nested / "services") in [c["path"] for c in found]

    def test_a_root_containing_the_provider_ranks_first(self, nested):
        found = core.suggest_roots(str(nested), provider="billing")
        assert found[0]["path"] == str(nested / "services")
        assert found[0]["has_provider"] is True

    def test_documentation_is_not_mistaken_for_services(self, nested):
        assert all(c["path"] != str(nested / "docs") for c in core.suggest_roots(str(nested)))

    def test_discover_offers_them_when_the_root_is_wrong(self, nested):
        """Pointed one level too high, it says where to point instead."""
        result = core.discover(str(nested), "billing", "anything")
        assert any(
            s["path"] == str(nested / "services") and s["has_provider"]
            for s in result["suggested_roots"]
        )

    def test_a_correct_root_gets_no_suggestions(self, at_repo_root):
        """Suggestions on a working setup would be noise."""
        result = core.discover("fixtures", "account-service", "customer_id", "account_id")
        assert result["suggested_roots"] == []


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

class TestDoctor:
    def test_it_succeeds_from_outside_the_source_tree(self, tmp_path, monkeypatch):
        """
        The bug that fired on the very first command a new user typed.

        The MCP server check was `Path("interlock_mcp/server.py").is_file()` -
        relative to the working directory, on a check marked non-optional. Run
        from your own repository, on a correct install, `doctor` reported MISS
        and exited non-zero.
        """
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "MISS" not in result.stdout

    def test_the_mcp_path_resolves_to_the_installed_package(self, tmp_path, monkeypatch):
        from interlock_cli.cli import _mcp_server_path

        monkeypatch.chdir(tmp_path)
        found = _mcp_server_path()
        assert found is not None and found.is_file()

    def test_it_reports_the_components_root(self, at_repo_root):
        result = runner.invoke(app, ["doctor", "--components-root", "fixtures"])
        assert result.exit_code == 0
        assert "5 component(s)" in result.stdout

    def test_a_missing_root_does_not_fail_the_run(self, tmp_path, monkeypatch):
        """
        Informational on purpose.

        `doctor` must never exit non-zero because a path unrelated to what the
        user is doing right now does not exist - that is precisely the bug
        above. It reports; `check` and `discover` decide.
        """
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor", "--components-root", str(tmp_path / "nope")])
        assert result.exit_code == 0
        assert "does not exist" in result.stdout

    def test_it_warns_when_the_workspace_would_copy_itself(self, tmp_path, monkeypatch):
        """
        The workspace root defaults to a *relative* `.interlock_work`, so
        running against `--components-root .` from inside your own repository
        makes each run copy the previous run's copy.
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor", "--components-root", "."])
        assert "WARNING" in result.stdout


# ---------------------------------------------------------------------------
# init — wiring another repository up to this installation
# ---------------------------------------------------------------------------

class TestInit:
    @pytest.fixture
    def their_repo(self, tmp_path):
        """A developer's own repository, services one level down."""
        repo = tmp_path / "shop"
        for name in ("orders", "billing"):
            service = repo / "services" / name
            service.mkdir(parents=True)
            (service / "main.py").write_text("x = 1\n", encoding="utf-8")
        return repo

    def test_it_writes_both_configs_with_absolute_paths(self, their_repo):
        """
        The configs this repo ships use a relative `.venv` and cwd, so they
        work only when Bob opens Interlock's own checkout. The generated entry
        must survive being launched from ANY working directory, because the MCP
        client — not Interlock — decides where the server starts.
        """
        result = core.init_mcp(str(their_repo), components_root="services")

        assert sorted(result["written"]) == sorted(
            [str(their_repo / ".bob" / "mcp.json"), str(their_repo / ".mcp.json")]
        )
        entry = json.loads(
            (their_repo / ".bob" / "mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["interlock"]
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", "interlock_mcp.server"]
        for value in entry["env"].values():
            assert Path(value).is_absolute()
        assert entry["env"]["INTERLOCK_COMPONENTS_ROOT"] == str(
            (their_repo / "services").resolve()
        )

    def test_bob_and_the_generic_config_launch_the_same_server(self, their_repo):
        core.init_mcp(str(their_repo), components_root="services")
        bob = json.loads(
            (their_repo / ".bob" / "mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["interlock"]
        generic = json.loads(
            (their_repo / ".mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["interlock"]
        for key in ("command", "args", "env"):
            assert bob[key] == generic[key]

    def test_it_preserves_other_servers(self, their_repo):
        """Their existing MCP configuration is not Interlock's to destroy."""
        (their_repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}),
            encoding="utf-8",
        )
        core.init_mcp(str(their_repo), components_root="services")
        servers = json.loads(
            (their_repo / ".mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]
        assert servers["github"] == {"command": "gh-mcp"}
        assert "interlock" in servers

    def test_it_refuses_to_clobber_a_file_it_cannot_parse(self, their_repo):
        """
        A hand-maintained config with a typo holds entries Interlock never
        wrote. Overwriting it would fix Interlock by destroying everything else.
        """
        (their_repo / ".mcp.json").write_text("{not json", encoding="utf-8")
        result = core.init_mcp(str(their_repo), components_root="services")

        assert (their_repo / ".mcp.json").read_text(encoding="utf-8") == "{not json"
        assert [s["path"] for s in result["skipped"]] == [
            str(their_repo / ".mcp.json")
        ]
        # The parseable file is still configured.
        assert result["written"] == [str(their_repo / ".bob" / "mcp.json")]

    def test_rerunning_replaces_only_the_interlock_entry(self, their_repo):
        core.init_mcp(str(their_repo), components_root="services")
        result = core.init_mcp(str(their_repo), components_root="services")
        assert sorted(result["replaced"]) == sorted(result["written"])
        servers = json.loads(
            (their_repo / ".mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]
        assert list(servers) == ["interlock"]

    def test_only_read_only_tools_are_auto_allowed(self):
        """
        `interlock_check` runs each component's declared test command. A tool
        that executes code must never be on an auto-allow list.
        """
        for tool in ("interlock_check", "interlock_start",
                     "interlock_approve_coordination"):
            assert tool not in core.MCP_AUTO_ALLOWED

    def test_a_missing_components_root_is_refused(self, their_repo):
        result = core.init_mcp(str(their_repo), components_root="nope")
        assert result["written"] == []
        assert any("nope" in p for p in result["problems"])

    def test_a_wrong_root_gets_the_same_suggestions_discover_gives(self, their_repo):
        """An empty root still writes the config, but says where to point."""
        (their_repo / "docs").mkdir()
        result = core.init_mcp(str(their_repo), components_root="docs")
        assert result["components"] == []
        assert any(
            s["path"] == str(their_repo / "services")
            for s in result["suggested_roots"]
        )

    def test_global_scope_writes_where_bob_actually_looks(self, tmp_path):
        """
        Bob's global file is `~/.bob/settings/mcp.json`. The obvious guess —
        `~/.bob/mcp.json`, which this project's own docs used to give — is
        silently ignored, and renders in Bob as "No MCP servers found".
        """
        result = core.init_mcp(".", scope="global", home=str(tmp_path))
        assert result["written"] == [
            str(tmp_path / ".bob" / "settings" / "mcp.json")
        ]
        entry = json.loads(
            (tmp_path / ".bob" / "settings" / "mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["interlock"]
        assert entry["command"] == sys.executable
        assert entry["env"]["INTERLOCK_DB_PATH"] == str(
            tmp_path / ".interlock" / "interlock.db"
        )
        # The ledger directory exists, so no first tool call can fail on it.
        assert (tmp_path / ".interlock").is_dir()

    def test_global_scope_defaults_to_the_bundled_fixtures(self, tmp_path):
        """A machine-wide entry has no target repo; the demo must still work."""
        result = core.init_mcp(".", scope="global", home=str(tmp_path))
        assert result["components_root"] == str(REPO_ROOT / "fixtures")
        assert "account-service" in result["components"]

    def test_a_ledger_in_a_missing_directory_creates_it(self, tmp_path):
        """
        The generated configs point INTERLOCK_DB_PATH into `.interlock/`, which
        need not exist yet. sqlite3 does not create parents, and "unable to
        open database file" on the first tool call is a terrible way to learn
        that.
        """
        conn = core.open_ledger(str(tmp_path / "deep" / "nested" / "ledger.db"))
        conn.execute("SELECT 1")

    def test_the_cli_reports_what_it_wrote(self, their_repo):
        result = runner.invoke(
            app,
            ["init", str(their_repo), "--components-root", "services"],
        )
        assert result.exit_code == 0
        assert ".bob" in result.stdout
        assert "orders, billing" in result.stdout or "billing, orders" in result.stdout

    def test_the_cli_exits_nonzero_when_nothing_was_written(self, tmp_path):
        result = runner.invoke(app, ["init", str(tmp_path / "absent")])
        assert result.exit_code == core.EXIT_ERROR


# ---------------------------------------------------------------------------
# Which MCP config would Bob actually use
# ---------------------------------------------------------------------------

class TestMcpClientStatus:
    """
    Bob deduplicates MCP servers by name: workspace overrides global. Its
    panel renders that as one row per scope, which reads as duplication, and a
    config at `~/.bob/mcp.json` — the obvious global location — is silently
    ignored, which reads as "No MCP servers found". Both questions must be
    answerable from the terminal.
    """

    def test_nothing_configured_says_how_to_start(self, tmp_path):
        status = core.mcp_client_status(cwd=str(tmp_path), home=str(tmp_path))
        assert status["configured"] is False
        assert "interlock init" in status["summary"]

    def test_global_only_is_every_workspace(self, tmp_path):
        core.init_mcp(".", scope="global", home=str(tmp_path))
        status = core.mcp_client_status(cwd=str(tmp_path), home=str(tmp_path))
        assert status["configured"] is True
        assert "every workspace" in status["summary"]

    def test_both_scopes_is_override_not_duplication(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "svc").mkdir(parents=True)
        core.init_mcp(str(repo), components_root=".")
        core.init_mcp(".", scope="global", home=str(tmp_path))
        status = core.mcp_client_status(cwd=str(repo), home=str(tmp_path))
        assert "override" in status["summary"]
        assert "duplication" in status["summary"]

    def test_workspace_only_warns_about_removal(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "svc").mkdir(parents=True)
        core.init_mcp(str(repo), components_root=".")
        status = core.mcp_client_status(cwd=str(repo), home=str(tmp_path))
        assert "removes the tools" in status["summary"]

    def test_the_ignored_global_location_is_flagged(self, tmp_path):
        """
        `~/.bob/mcp.json` looked exactly right and Bob showed "No MCP servers
        found" — the failure this whole check exists to catch.
        """
        (tmp_path / ".bob").mkdir()
        (tmp_path / ".bob" / "mcp.json").write_text("{}", encoding="utf-8")
        status = core.mcp_client_status(cwd=str(tmp_path), home=str(tmp_path))
        assert status["misplaced_global"] is True
        assert status["configured"] is False

    def test_a_stale_interpreter_path_is_a_problem(self, tmp_path):
        """The venv moved or was deleted: the entry exists, the command doesn't."""
        core.init_mcp(
            ".", scope="global", home=str(tmp_path), python=str(tmp_path / "gone.exe")
        )
        status = core.mcp_client_status(cwd=str(tmp_path), home=str(tmp_path))
        assert "does not exist" in (status["global"]["problem"] or "")


# ---------------------------------------------------------------------------
# The workspace copy
# ---------------------------------------------------------------------------

class TestWorkspaceCopy:
    def test_dependency_caches_are_not_copied(self):
        from orchestrator.real_workflow import _copy_ignore

        ignored = _copy_ignore()
        for name in ("node_modules", ".venv", "venv", "target", "build", "dist", ".git"):
            assert name in ignored, name

    def test_it_leaves_them_behind_in_practice(self, tmp_path, monkeypatch):
        """
        Copying `node_modules` put `git add .` over its 60-second cap. The
        timeout is swallowed, `_is_usable` only tests that `.git` is a
        directory, and the HEAD-less workspace was then reused forever -
        surfacing as "no git commit recorded" on every component.
        """
        from orchestrator.real_workflow import prepare_workspace

        source = tmp_path / "src"
        (source / "svc" / "node_modules" / "left-pad").mkdir(parents=True)
        (source / "svc" / "node_modules" / "left-pad" / "index.js").write_text(
            "module.exports = 1\n", encoding="utf-8"
        )
        (source / "svc" / "app.py").write_text("x = 1\n", encoding="utf-8")

        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))
        workspace = prepare_workspace("copy-test", str(source))

        assert (workspace / "svc" / "app.py").is_file()
        assert not (workspace / "svc" / "node_modules").exists()

    def test_the_workspace_does_not_copy_itself(self, tmp_path, monkeypatch):
        """
        The workspace root defaults to a *relative* `.interlock_work`, so
        `--components-root .` from inside your own repository puts the
        destination inside the source. `copytree` snapshots the listing after
        the destination exists, so run 2 copies run 1's copy, run 3 copies
        that, and the tree grows exponentially. The only symptom is that things
        get slower until the disk fills.

        This is not hypothetical - writing these tests triggered it.
        """
        from orchestrator.real_workflow import prepare_workspace

        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "app.py").write_text("x = 1\n", encoding="utf-8")

        # The workspace lives INSIDE the components root.
        monkeypatch.setenv("INTERLOCK_WORKSPACE", str(tmp_path / "work"))

        first = prepare_workspace("run-1", str(tmp_path))
        second = prepare_workspace("run-2", str(tmp_path))

        assert (second / "svc" / "app.py").is_file()
        assert not (first / "work").exists()
        assert not (second / "work").exists()

    def test_a_component_with_files_must_end_up_with_a_commit(self, tmp_path):
        """
        The safety net for when the baseline commit fails anyway.

        Every implementation agent reads a real commit SHA back out of these
        repositories, and without a HEAD each reports "no git commit recorded"
        - true, but three layers from the cause.
        """
        from orchestrator.real_workflow import _verify_baseline

        component = tmp_path / "svc"
        component.mkdir()
        (component / "app.py").write_text("x = 1\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="baseline commit failed"):
            _verify_baseline([component])

    def test_an_empty_component_is_not_an_error(self, tmp_path):
        """git cannot commit nothing, and an empty directory is not a failure."""
        from orchestrator.real_workflow import _verify_baseline

        component = tmp_path / "empty"
        component.mkdir()
        _verify_baseline([component])
