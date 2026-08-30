"""
orchestrator/manifest.py
=========================
Per-component manifest: how to test a component, whatever language it is in.

Why this exists
---------------
`contract_test` used to hardcode `python -m pytest`. That single line was what
confined Interlock to Python codebases — not the gate, not discovery, not the
evidence model, all of which are language-neutral. A component that declares

    [component]
    language = "c"
    test_command = "make test"

can be verified by exactly the same machinery: the agent does not need to
understand C, only whether the component's own suite passes.

This is what makes a C-to-Python transition expressible. Interlock does not
translate the code — it proves that every consumer still passes its own tests
and that the old path is not retired until they do.

Absent manifest
---------------
Falls back to running pytest against the component directory, which is what the
Python fixtures already relied on. Existing components therefore need no
manifest, and adding one is how you opt a component into another toolchain.
"""

from __future__ import annotations

import shlex
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILENAME = "interlock.toml"


# ---------------------------------------------------------------------------
# Program resolution
# ---------------------------------------------------------------------------

# POSIX tools that Git for Windows ships but does not put on the system PATH.
_GIT_BUNDLED = {"sh", "bash", "make", "awk", "sed", "grep"}


def _git_usr_bin() -> Path | None:
    """
    Git for Windows' `usr/bin`, found from the git executable itself.

    Located relative to git rather than by guessing install paths, so a
    portable or non-default installation still works. Returns None on any
    platform where this does not apply.
    """
    import shutil

    git = shutil.which("git")
    if not git:
        return None
    # Git installs git.exe at <root>/cmd/git.exe (parents[1]) or at
    # <root>/mingw64/bin/git.exe (parents[2]); PATH may point at either.
    for parents in (1, 2, 3):
        try:
            candidate = Path(git).resolve().parents[parents] / "usr" / "bin"
        except IndexError:
            continue
        if candidate.is_dir():
            return candidate
    return None


def resolve_program(argv: list[str]) -> list[str]:
    """
    Return *argv* with its program resolved to a real executable where possible.

    Leaves argv untouched when the program is already resolvable, and returns
    it unchanged when it cannot be found at all - the failure then surfaces at
    execution, where it is attributable and recorded as evidence, rather than
    here where it would abort a whole change.
    """
    import shutil

    if not argv:
        return argv
    program = argv[0]
    if Path(program).is_absolute() or shutil.which(program):
        return argv
    if program in _GIT_BUNDLED:
        usr_bin = _git_usr_bin()
        if usr_bin is not None:
            for suffix in (".exe", ""):
                candidate = usr_bin / f"{program}{suffix}"
                if candidate.is_file():
                    return [str(candidate), *argv[1:]]
    return argv


def environment_for(argv: list[str]) -> dict[str, str] | None:
    """
    The environment *argv* should run in, or None to inherit unchanged.

    Resolving `sh` to Git's bundled `sh.exe` is only half the job. That shell
    starts with the *caller's* PATH, which outside Git Bash does not contain
    `grep`, `sed` or `awk` - so a perfectly ordinary POSIX test script dies
    with "grep: command not found" and reports a test failure that has nothing
    to do with the tests.

    Prepending the same `usr/bin` that provided the shell reproduces what Git
    Bash does, so a script behaves the way its author expects. Only applied
    when the command actually resolves into that directory: nothing else has
    its PATH rewritten.
    """
    import os

    if not argv:
        return None
    usr_bin = _git_usr_bin()
    if usr_bin is None:
        return None
    program = Path(resolve_program(argv)[0])
    if program.parent != usr_bin:
        return None
    env = dict(os.environ)
    env["PATH"] = f"{usr_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def missing_program(argv: list[str]) -> str | None:
    """The program name if it cannot be executed, else None. For diagnostics."""
    import shutil

    if not argv:
        return None
    program = resolve_program(argv)[0]
    if Path(program).is_absolute():
        return None if Path(program).is_file() else program
    return None if shutil.which(program) else program


# What a component gets when it declares nothing: run pytest against its own
# directory, using the interpreter Interlock is running under.
_DEFAULT_TEST_COMMAND: list[str] = [sys.executable, "-m", "pytest", ".", "-v", "--tb=short"]
_DEFAULT_LANGUAGE = "python"


@dataclass(frozen=True)
class ComponentManifest:
    """How to build, test and identify one component."""

    language: str = _DEFAULT_LANGUAGE
    test_command: list[str] | None = None
    coexistence_command: list[str] | None = None
    """
    How to prove this provider serves the old and new paths simultaneously.

    The default rehearsal starts the component as an ASGI app and probes it over
    HTTP, which assumes the provider is a web service. A C library, a batch job
    or a message publisher is not, and trying to `uvicorn service:app` one exits
    immediately. Declaring a command here lets any provider prove coexistence in
    whatever way is meaningful for it.
    """
    declared: bool = False
    """True when an interlock.toml was actually found and parsed."""

    def command(self) -> list[str]:
        """
        The argv to run this component's tests.

        The built-in pytest default is made hermetic. When the component sits
        inside a directory tree that carries a pytest.ini — Interlock's own
        test runs place agent workspaces under `.pytest_tmp/` — the inner
        pytest inherits that ini's `--basetemp=.pytest_tmp`, and pytest
        DELETES its basetemp at session start. Every agent-run inner pytest
        was silently wiping the outer test session's live temp directories,
        which surfaced as unrelated tests failing with missing files on every
        full-suite run. A private basetemp and no cache plugin make the inner
        run unable to touch anyone else's state.

        A manifest-declared command is the component's own business and is
        passed through untouched.

        Returns what the manifest DECLARES, not what will be executed.
        `resolve_program()` is applied at the point of execution instead, so
        evidence records the portable command its author wrote rather than an
        absolute path that is true only on the machine that happened to run it.
        """
        if self.test_command:
            return list(self.test_command)
        return list(_DEFAULT_TEST_COMMAND) + [
            "-p", "no:cacheprovider",
            "--basetemp", str(Path(tempfile.mkdtemp(prefix="interlock-bt-")) / "bt"),
        ]

    @property
    def uses_default_pytest(self) -> bool:
        """
        True when we are running the built-in pytest default.

        Callers use this to decide whether pytest-specific exit codes are
        meaningful — exit 5 means "no tests collected" for pytest and means
        nothing in particular for `make test`.
        """
        return not self.test_command


def load(component_dir: Path) -> ComponentManifest:
    """
    Read `interlock.toml` from *component_dir*.

    A malformed or unreadable manifest yields the default rather than raising:
    the correct place to fail loudly is the test run itself, where the failure
    is attributable and shows up as evidence. A parse error here would abort a
    whole change for a typo in one component's config.
    """
    path = Path(component_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return ComponentManifest()

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ComponentManifest()

    section = data.get("component")
    if not isinstance(section, dict):
        return ComponentManifest()

    # A string is split the way a shell would, but executed WITHOUT a shell so a
    # component manifest cannot smuggle in `rm -rf` via a pipe or `;`.
    command = _as_command(section.get("test_command"))

    language = section.get("language")
    return ComponentManifest(
        language=str(language) if language else _DEFAULT_LANGUAGE,
        test_command=command,
        coexistence_command=_as_command(section.get("coexistence_command")),
        declared=True,
    )


def _as_command(raw: object) -> list[str] | None:
    """Accept a shell-style string or an argv list; never execute via a shell."""
    if isinstance(raw, str) and raw.strip():
        return shlex.split(raw)
    if isinstance(raw, list) and raw:
        return [str(part) for part in raw]
    return None
