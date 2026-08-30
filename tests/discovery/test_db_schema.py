"""
tests/discovery/test_db_schema.py
====================================
Tests for agents/discovery/db_schema.py

Proves:
  1. platform-config is discovered as a DB-schema consumer.
  2. The source_ref cites a real SQL file at a real line number.
  3. The cited line actually contains 'customer_id'.
  4. Result validates as DiscoveryResult.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.discovery import db_schema
from orchestrator.schemas import DiscoveryResult


@pytest.fixture(scope="module")
def db_result(base_data):
    return db_schema.run(base_data)


@pytest.fixture(scope="module")
def db_dependencies(db_result):
    return db_result["dependencies"]


class TestPlatformConfigDiscovered:
    def test_finds_platform_config_as_db_consumer(self, db_dependencies):
        """platform-config must appear with edge_type='db'."""
        db_edges = [
            d for d in db_dependencies
            if d["to_component"] == "platform-config" and d["edge_type"] == "db"
        ]
        assert db_edges, (
            "Expected a db-type dependency edge to 'platform-config'. "
            "Check that platform-config/schema.sql contains 'customer_id'."
        )

    def test_provider_is_account_service(self, db_dependencies):
        """DB dependency edges must originate from account-service."""
        for dep in db_dependencies:
            assert dep["from_component"] == "account-service", (
                f"Expected from_component='account-service', got {dep['from_component']}"
            )


class TestDBSourceRef:
    def test_source_ref_is_real_path(self, db_result, fixtures_root):
        """Every source_ref must resolve to an existing file."""
        for ev in db_result["evidence"]:
            file_part = ev["source_ref"].split(":")[0]
            resolved = (fixtures_root / file_part).resolve()
            assert resolved.exists(), (
                f"source_ref '{ev['source_ref']}' points to non-existent path"
            )

    def test_source_ref_points_to_sql_file(self, db_result, fixtures_root):
        """The source_ref for platform-config must point to a .sql file."""
        pc_ev = next(
            (e for e in db_result["evidence"] if e["subject"] == "platform-config"),
            None,
        )
        assert pc_ev is not None, "No evidence found for platform-config"
        file_part = pc_ev["source_ref"].split(":")[0]
        assert file_part.endswith(".sql"), (
            f"Expected a .sql file, got: {file_part}"
        )

    def test_source_ref_line_contains_customer_id(self, db_result, fixtures_root):
        """
        Open the cited SQL file at the cited line and confirm 'customer_id'
        is present on that line.  Proves evidence is grounded in real source.
        """
        pc_ev = next(
            (e for e in db_result["evidence"] if e["subject"] == "platform-config"),
            None,
        )
        assert pc_ev is not None, "No evidence found for platform-config"

        ref = pc_ev["source_ref"]
        file_part, _, lineno_str = ref.rpartition(":")
        line_idx = int(lineno_str) - 1

        file_path = (fixtures_root / file_part).resolve()
        assert file_path.exists(), f"source_ref file does not exist: {file_path}"

        lines = file_path.read_text(encoding="utf-8").splitlines()
        assert 0 <= line_idx < len(lines), (
            f"source_ref line {lineno_str} out of range for {file_part} "
            f"(file has {len(lines)} lines)"
        )
        assert "customer_id" in lines[line_idx], (
            f"Expected 'customer_id' at {ref}, got: {lines[line_idx]!r}"
        )

    def test_multiple_customer_id_refs_in_schema(self, db_result):
        """
        platform-config/schema.sql has several customer_id columns —
        evidence should reflect multiple references, not just one.
        """
        pc_ev = next(
            (e for e in db_result["evidence"] if e["subject"] == "platform-config"),
            None,
        )
        assert pc_ev is not None, "No evidence found for platform-config"
        refs = pc_ev["content"].get("refs", [])
        assert len(refs) >= 3, (
            f"Expected at least 3 customer_id references in schema.sql, found {len(refs)}"
        )


class TestDBResultShape:
    def test_result_validates_as_discovery_result(self, db_result):
        """Output must parse cleanly as DiscoveryResult."""
        validated = DiscoveryResult(**db_result)
        assert validated.change_id == "test-discovery-001"

    def test_evidence_includes_schema_files_list(self, db_result):
        """Evidence content must list the schema files that were inspected."""
        for ev in db_result["evidence"]:
            assert "schema_files" in ev["content"], (
                f"Evidence for {ev['subject']} missing 'schema_files' key"
            )
            assert isinstance(ev["content"]["schema_files"], list)
