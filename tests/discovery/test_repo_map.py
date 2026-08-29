"""
tests/discovery/test_repo_map.py
==================================
Tests for agents/discovery/repo_map.py

Proves:
  1. repo-map finds all five fixture repositories.
  2. Result validates as DiscoveryResult.
  3. Every source_ref in evidence points to a real, existing file path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.discovery import repo_map
from orchestrator.schemas import DiscoveryResult

# The five fixture repositories we expect to discover
EXPECTED_REPOS = {
    "account-service",
    "analytics-worker",
    "checkout",
    "fraud",
    "platform-config",
}


@pytest.fixture(scope="module")
def repo_map_result(base_data):
    return repo_map.run(base_data)


class TestRepoMapFindsAllRepos:
    def test_finds_all_five_repos(self, repo_map_result):
        """repo-map must surface all five fixture repositories."""
        found_components = {e["subject"] for e in repo_map_result["evidence"]}
        assert EXPECTED_REPOS <= found_components, (
            f"Missing repos: {EXPECTED_REPOS - found_components}"
        )

    def test_no_unexpected_extra_repos(self, repo_map_result):
        """repo-map should not invent fixture repos that don't exist."""
        found_components = {e["subject"] for e in repo_map_result["evidence"]}
        unexpected = found_components - EXPECTED_REPOS
        assert not unexpected, f"Unexpected repos found: {unexpected}"


class TestRepoMapResultShape:
    def test_result_validates_as_discovery_result(self, repo_map_result):
        """Output must parse cleanly as DiscoveryResult (no extra fields, no missing)."""
        validated = DiscoveryResult(**repo_map_result)
        assert validated.change_id == "test-discovery-001"

    def test_evidence_list_non_empty(self, repo_map_result):
        assert len(repo_map_result["evidence"]) >= 5

    def test_dependencies_list_present(self, repo_map_result):
        assert "dependencies" in repo_map_result
        assert isinstance(repo_map_result["dependencies"], list)


class TestRepoMapSourceRefs:
    def test_source_refs_are_real_paths(self, repo_map_result, fixtures_root):
        """
        Every source_ref must resolve to an existing file.
        source_ref format: "relative/path/to/file.py:lineno"
        """
        for ev in repo_map_result["evidence"]:
            raw_ref = ev["source_ref"]
            # Strip optional :lineno suffix
            file_part = raw_ref.split(":")[0]
            resolved = (fixtures_root / file_part).resolve()
            assert resolved.exists(), (
                f"source_ref '{raw_ref}' points to non-existent path: {resolved}"
            )

    def test_field_refs_in_content_have_real_paths(self, repo_map_result, fixtures_root):
        """
        Each field_ref dict inside evidence content must point to an existing file.
        """
        for ev in repo_map_result["evidence"]:
            for ref in ev["content"].get("field_refs", []):
                file_path = (fixtures_root / ref["file"]).resolve()
                assert file_path.exists(), (
                    f"field_ref file '{ref['file']}' does not exist"
                )

    def test_field_refs_contain_customer_id(self, repo_map_result, fixtures_root):
        """
        For each field_ref, open the cited file at the cited line and confirm
        'customer_id' is present on that line.
        """
        for ev in repo_map_result["evidence"]:
            for ref in ev["content"].get("field_refs", []):
                file_path = (fixtures_root / ref["file"]).resolve()
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                line_idx = ref["line"] - 1  # convert 1-based to 0-based
                assert 0 <= line_idx < len(lines), (
                    f"Line {ref['line']} out of range in {ref['file']}"
                )
                assert "customer_id" in lines[line_idx], (
                    f"Expected 'customer_id' at {ref['file']}:{ref['line']}, "
                    f"got: {lines[line_idx]!r}"
                )
