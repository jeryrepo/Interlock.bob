"""
Shared pytest fixtures.

Two Windows-specific issues are addressed here:

1. SQLite handle leak.  CliRunner retains tracebacks; those frames keep the
   `conn` alive; on Windows an open SQLite handle blocks shutil.rmtree on the
   *next* test's tmp_path setup.  `_close_cli_ledgers` + gc.collect() clears
   every connection opened through core.open_ledger().

2. Read-only .git files.  Real-agent tests copy fixture trees into tmp_path,
   which includes .git directories.  git marks pack files and object files
   read-only on Windows, so plain shutil.rmtree raises PermissionError when
   pytest tries to delete the directory before the next test.
   `_force_tmp_cleanup` installs a stat.S_IWRITE retry as the shutil onexc
   handler so those deletions succeed.
"""

from __future__ import annotations

import gc
import os
import shutil
import stat

import pytest


def _clear_readonly(func, target, _exc):
    """shutil.rmtree onexc handler: clear read-only bit and retry."""
    try:
        os.chmod(target, stat.S_IWRITE)
        func(target)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _close_cli_ledgers():
    yield
    try:
        from interlock_cli import core
    except ImportError:
        return
    core.close_ledgers()
    gc.collect()


@pytest.fixture(autouse=True)
def _force_tmp_cleanup(tmp_path):
    """
    After each test, remove the tmp_path tree with read-only-aware cleanup.

    This runs after the test body, so any subprocess that left read-only .git
    objects behind is already finished.  The fixture is autouse so no test
    needs to opt in, and it is harmless when tmp_path contains no git trees.
    """
    yield
    if tmp_path.exists():
        shutil.rmtree(tmp_path, onexc=_clear_readonly)
