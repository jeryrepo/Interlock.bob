"""
tests/orchestrator/test_toolchain.py
=====================================
Tests for detecting how a component is built, and for running what it declares.

Two failures motivated this file, both invisible against the bundled fixtures:

**A component with no manifest was silently assumed to be Python.** On a Go or
Java service `python -m pytest .` collects nothing, exits 5, and is recorded as
`tests_could_not_run`. The verdict was correct — nothing was proven — but the
reason pointed at Interlock rather than at the missing three-line file, and on a
polyglot repository that was every component at once.

**A declared command could not find its own program.** `test_command = "sh
run_tests.sh"` works in Git Bash and exits 127 from PowerShell, because Git for
Windows ships `sh.exe` without putting it on the system PATH. Interlock reported
that as "tests failed", which reads as "your code is broken" when the truth is
"the runner is not on PATH". Resolving the program was still not enough: the
shell then started with a PATH that had no `grep`, so an ordinary POSIX test
script died on its first line.

Detection never reaches the gate. It proposes a manifest for a human to read;
what actually runs is whatever `interlock.toml` says.
"""

from __future__ import annotations

import sys

import pytest

from orchestrator import manifest as manifest_mod
from orchestrator import toolchain


def _component(tmp_path, name: str, files: dict[str, str]):
    directory = tmp_path / name
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestMarkerDetection:
    @pytest.mark.parametrize("files,language,command", [
        ({"go.mod": "module x\n", "main.go": "package main\n"}, "go", "go test ./..."),
        ({"Cargo.toml": "[package]\n", "src.rs": "fn main(){}\n"}, "rust", "cargo test"),
        ({"pom.xml": "<project/>\n", "A.java": "class A{}\n"}, "java", "mvn -B test"),
        ({"build.gradle": "\n", "A.java": "class A{}\n"}, "java", "gradle test"),
        ({"x.csproj": "<Project/>\n", "A.cs": "class A{}\n"}, "csharp", "dotnet test"),
        ({"CMakeLists.txt": "\n", "a.c": "int main(){}\n"}, "c", "ctest"),
    ])
    def test_a_build_file_identifies_the_toolchain(
        self, tmp_path, files, language, command
    ):
        found = toolchain.detect(_component(tmp_path, "svc", files))
        assert found.language == language
        assert found.test_command == command
        assert found.confidence == "marker"

    def test_a_gradle_wrapper_is_preferred_over_a_global_gradle(self, tmp_path):
        directory = _component(tmp_path, "svc", {
            "build.gradle": "\n", "gradlew": "#!/bin/sh\n", "A.java": "class A{}\n",
        })
        assert toolchain.detect(directory).test_command == "./gradlew test"

    def test_a_makefile_counts_only_with_a_test_target(self, tmp_path):
        without = _component(tmp_path, "a", {"Makefile": "build:\n\tcc x.c\n",
                                             "x.c": "int main(){}\n"})
        assert toolchain.detect(without).test_command is None

        with_target = _component(tmp_path, "b", {"Makefile": "test:\n\t./run\n",
                                                 "x.c": "int main(){}\n"})
        assert toolchain.detect(with_target).test_command == "make test"


class TestNodeDetection:
    def test_a_declared_test_script_is_used(self, tmp_path):
        directory = _component(tmp_path, "svc", {
            "package.json": '{"scripts":{"test":"jest --ci"}}',
            "index.ts": "export const k = 1;\n",
        })
        found = toolchain.detect(directory)
        assert found.language == "typescript"
        assert found.test_command == "npm test"
        assert found.confidence == "declared"

    def test_npms_placeholder_script_is_not_mistaken_for_a_real_one(self, tmp_path):
        """
        `npm init` writes a `test` script that only exits 1. Treating that as a
        test command would report every fresh package as a failing suite.
        """
        directory = _component(tmp_path, "svc", {
            "package.json":
                '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}',
            "app.js": "const x = 1;\n",
        })
        found = toolchain.detect(directory)
        assert found.test_command is None
        assert any("no real test script" in n for n in found.notes)

    def test_a_package_without_any_test_script_says_so(self, tmp_path):
        directory = _component(tmp_path, "svc", {
            "package.json": '{"name":"x"}', "app.js": "const x = 1;\n",
        })
        found = toolchain.detect(directory)
        assert found.test_command is None
        assert any("no scripts.test" in n for n in found.notes)


class TestPythonAndFallbacks:
    def test_a_pytest_layout_needs_no_manifest(self, tmp_path):
        """The built-in default already is pytest; a manifest would restate it."""
        directory = _component(tmp_path, "svc", {
            "app.py": "x = 1\n", "tests/test_app.py": "def test_x(): pass\n",
        })
        found = toolchain.detect(directory)
        assert found.language == "python"
        assert toolchain.needs_manifest(found) is False

    def test_a_non_python_component_always_needs_one(self, tmp_path):
        directory = _component(tmp_path, "svc", {"go.mod": "module x\n"})
        assert toolchain.needs_manifest(toolchain.detect(directory)) is True

    def test_source_with_no_build_file_is_a_guess_with_no_command(self, tmp_path):
        directory = _component(tmp_path, "svc", {"main.c": "int main(){}\n"})
        found = toolchain.detect(directory)
        assert found.language == "c"
        assert found.test_command is None
        assert found.confidence == "guess"

    def test_an_unrecognisable_directory_does_not_raise(self, tmp_path):
        directory = _component(tmp_path, "svc", {"notes.txt": "hello\n"})
        found = toolchain.detect(directory)
        assert found.language == "unknown"
        assert found.test_command is None

    def test_a_missing_directory_does_not_raise(self, tmp_path):
        assert toolchain.detect(tmp_path / "nope").language == "unknown"

    def test_a_shell_runner_does_not_mask_the_real_language(self, tmp_path):
        """
        `sh run_tests.sh` is how a C library is tested, not evidence that it is
        written in shell. Recording "shell" would be actively misleading in the
        manifest this generates.
        """
        directory = _component(tmp_path, "svc", {
            "run_tests.sh": "#!/bin/sh\nexit 0\n", "calc.c": "int main(){}\n",
        })
        found = toolchain.detect(directory)
        assert found.test_command == "sh run_tests.sh"
        assert found.language == "c"

    def test_a_mixed_component_is_flagged(self, tmp_path):
        """
        One component gets one test command. A directory holding both a Go
        service and a React front end must either declare a command covering
        both or be split - saying so now is cheaper than finding out when half
        the suite silently never ran.
        """
        directory = _component(tmp_path, "svc", {
            "go.mod": "module x\n", "main.go": "package main\n",
            "ui/app.tsx": "export const A = 1;\n",
        })
        assert any("also contains" in n for n in toolchain.detect(directory).notes)


class TestGeneratedToml:
    def test_it_parses_back_to_the_detected_command(self, tmp_path):
        directory = _component(tmp_path, "svc", {"go.mod": "module x\n"})
        (directory / "interlock.toml").write_text(
            toolchain.detect(directory).to_toml(), encoding="utf-8"
        )
        loaded = manifest_mod.load(directory)
        assert loaded.declared is True
        assert loaded.language == "go"
        assert loaded.test_command == ["go", "test", "./..."]

    def test_an_undetectable_command_is_left_blank_not_guessed(self, tmp_path):
        """A wrong command that runs is worse than an obvious blank."""
        directory = _component(tmp_path, "svc", {"main.c": "int main(){}\n"})
        generated = toolchain.detect(directory).to_toml()
        assert "# test_command" in generated
        (directory / "interlock.toml").write_text(generated, encoding="utf-8")
        assert manifest_mod.load(directory).test_command is None


# ---------------------------------------------------------------------------
# manifest_plan
# ---------------------------------------------------------------------------

class TestManifestPlan:
    @pytest.fixture
    def repo(self, tmp_path):
        _component(tmp_path, "go-svc", {"go.mod": "module x\n", "m.go": "package main\n"})
        _component(tmp_path, "py-svc", {"app.py": "x=1\n",
                                        "tests/test_a.py": "def test(): pass\n"})
        _component(tmp_path, "mystery", {"main.c": "int main(){}\n"})
        return tmp_path

    def test_preview_writes_nothing(self, repo):
        from interlock_cli import core

        plan = core.manifest_plan(str(repo))
        assert plan["written"] == []
        assert not (repo / "go-svc" / "interlock.toml").exists()
        assert {e["name"]: e["action"] for e in plan["components"]} == {
            "go-svc": "would write",
            "mystery": "would write (incomplete)",
            "py-svc": "not needed",
        }

    def test_write_creates_only_what_is_needed(self, repo):
        from interlock_cli import core

        plan = core.manifest_plan(str(repo), write=True)
        assert (repo / "go-svc" / "interlock.toml").is_file()
        assert (repo / "mystery" / "interlock.toml").is_file()
        # pytest layout is already covered by the built-in default.
        assert not (repo / "py-svc" / "interlock.toml").exists()
        assert len(plan["written"]) == 2

    def test_an_existing_manifest_is_never_overwritten(self, repo):
        """
        A hand-written manifest encodes a decision someone made about how their
        component is tested. A guess derived from a build file must not replace
        it silently.
        """
        from interlock_cli import core

        mine = repo / "go-svc" / "interlock.toml"
        mine.write_text(
            '[component]\nlanguage = "go"\ntest_command = "go test -race ./..."\n',
            encoding="utf-8",
        )
        core.manifest_plan(str(repo), write=True)
        assert "-race" in mine.read_text(encoding="utf-8")

    def test_writing_twice_is_idempotent(self, repo):
        from interlock_cli import core

        core.manifest_plan(str(repo), write=True)
        second = core.manifest_plan(str(repo), write=True)
        assert second["written"] == []
        assert all(e["action"] in ("kept", "not needed") for e in second["components"])


# ---------------------------------------------------------------------------
# Running what a manifest declares
# ---------------------------------------------------------------------------

class TestProgramResolution:
    def test_an_absolute_program_is_left_alone(self):
        argv = [sys.executable, "-c", "pass"]
        assert manifest_mod.resolve_program(argv) == argv

    def test_an_unknown_program_is_left_for_execution_to_report(self):
        """
        Failing here would abort the whole change for one component. Failing at
        execution is attributable and recorded as evidence.
        """
        argv = ["definitely-not-a-real-program-xyz", "test"]
        assert manifest_mod.resolve_program(argv) == argv
        assert manifest_mod.missing_program(argv) == "definitely-not-a-real-program-xyz"

    def test_an_empty_command_is_handled(self):
        assert manifest_mod.resolve_program([]) == []
        assert manifest_mod.missing_program([]) is None

    @pytest.mark.skipif(
        manifest_mod._git_usr_bin() is None,
        reason="Git's bundled POSIX tools are not present",
    )
    def test_a_posix_shell_is_found_even_when_it_is_not_on_path(self):
        """
        `sh run_tests.sh` exits 127 from PowerShell because Git ships sh.exe
        without putting it on the system PATH. Interlock already requires git,
        so the shell that ships with it is a dependency already present.
        """
        assert manifest_mod.missing_program(["sh", "x.sh"]) is None

    @pytest.mark.skipif(
        manifest_mod._git_usr_bin() is None,
        reason="Git's bundled POSIX tools are not present",
    )
    def test_a_bundled_shell_gets_the_posix_tools_on_its_path(self):
        """
        Resolving the shell is half the job: it then starts with the caller's
        PATH, which outside Git Bash has no `grep`, so an ordinary POSIX script
        dies on its first line and reports a test failure it did not have.
        """
        import shutil

        resolved = manifest_mod.resolve_program(["sh", "x.sh"])[0]
        if shutil.which("sh"):
            pytest.skip("sh is already on PATH; nothing to repair")
        env = manifest_mod.environment_for(["sh", "x.sh"])
        assert env is not None
        assert str(manifest_mod._git_usr_bin()) in env["PATH"]
        assert resolved.endswith(("sh.exe", "sh"))

    def test_an_ordinary_command_inherits_the_environment_unchanged(self):
        """Only a bundled shell gets its PATH rewritten; nothing else."""
        assert manifest_mod.environment_for([sys.executable, "-m", "pytest"]) is None

    def test_the_manifest_reports_the_declared_command_not_the_resolved_one(
        self, tmp_path
    ):
        """
        Evidence must record the portable command its author wrote. An absolute
        path is true only on the machine that happened to run it, and it would
        leak local paths into a PR comment.
        """
        directory = _component(tmp_path, "svc", {
            "interlock.toml": '[component]\nlanguage = "c"\ntest_command = "sh t.sh"\n',
        })
        assert manifest_mod.load(directory).command() == ["sh", "t.sh"]


class TestMissingRunnerIsNotATestFailure:
    def test_contract_test_names_the_missing_program(self, tmp_path):
        from agents.verification import contract_test

        directory = _component(tmp_path, "svc", {
            "interlock.toml":
                '[component]\nlanguage = "go"\n'
                'test_command = "definitely-not-a-real-program-xyz test"\n',
        })
        result = contract_test.run({"change_id": "c1"}, directory)
        content = result["evidence"][0]["content"]
        assert result["status"] == "failed"
        assert content["outcome"] == "tests_could_not_run"
        assert "definitely-not-a-real-program-xyz" in content["output_tail"]
        assert "not found on PATH" in content["output_tail"]
        # The declared command survives into evidence, so a reader can see
        # exactly what the manifest asked for.
        assert content["test_command"] == ["definitely-not-a-real-program-xyz", "test"]
