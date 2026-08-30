"""
tests/discovery/test_event_contract.py
=========================================
Tests for agents/discovery/event_contract.py

Proves:
  1. analytics-worker is discovered as an event consumer.
  2. The source_ref cites a real line number where 'customer_id' appears.
  3. REGRESSION: removing event["customer_id"] from worker.py removes the
     dependency — the agent must not find analytics-worker if the source
     reference is absent.  The real fixture is never mutated; a tmp_path
     copy is used.
  4. checkout and fraud are NOT discovered as event consumers.
  5. Result validates as DiscoveryResult.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agents.discovery import event_contract
from orchestrator.schemas import DiscoveryResult


@pytest.fixture(scope="module")
def event_result(base_data):
    return event_contract.run(base_data)


@pytest.fixture(scope="module")
def event_dependencies(event_result):
    return event_result["dependencies"]


class TestAnalyticsWorkerDiscovered:
    def test_finds_analytics_worker_as_event_consumer(self, event_dependencies):
        """
        analytics-worker must appear as an event consumer with edge_type='event'.
        This is the critical undocumented dependency that must be found from source.
        """
        event_edges = [
            d for d in event_dependencies
            if d["to_component"] == "analytics-worker"
            and d["edge_type"] == "event"
        ]
        assert event_edges, (
            "analytics-worker was not discovered as an event consumer. "
            "Check that worker.py contains event['customer_id'] inside "
            "a function whose name contains 'event'."
        )

    def test_provider_is_account_service(self, event_dependencies):
        """The event edge must originate from account-service."""
        event_edges = [
            d for d in event_dependencies if d["edge_type"] == "event"
        ]
        assert event_edges, "No event-type dependencies found at all"
        for edge in event_edges:
            assert edge["from_component"] == "account-service"

    def test_only_analytics_worker_is_event_consumer(self, event_dependencies):
        """checkout and fraud must NOT be classified as event consumers."""
        event_consumers = {
            d["to_component"]
            for d in event_dependencies
            if d["edge_type"] == "event"
        }
        assert "checkout" not in event_consumers, (
            "checkout was incorrectly classified as an event consumer"
        )
        assert "fraud" not in event_consumers, (
            "fraud was incorrectly classified as an event consumer"
        )


class TestEventSourceRef:
    def test_source_ref_is_real_path(self, event_result, fixtures_root):
        """Every source_ref must resolve to an existing file."""
        for ev in event_result["evidence"]:
            file_part = ev["source_ref"].split(":")[0]
            resolved = (fixtures_root / file_part).resolve()
            assert resolved.exists(), (
                f"source_ref '{ev['source_ref']}' points to non-existent path"
            )

    def test_source_ref_line_contains_customer_id(self, event_result, fixtures_root):
        """
        Open the cited file at the cited line number and confirm the literal
        'customer_id' is present on that exact line.
        This proves the evidence is grounded in real source code.
        """
        analytics_ev = next(
            (e for e in event_result["evidence"] if e["subject"] == "analytics-worker"),
            None,
        )
        assert analytics_ev is not None, "No evidence entry found for analytics-worker"

        ref = analytics_ev["source_ref"]
        file_part, _, lineno_str = ref.rpartition(":")
        line_idx = int(lineno_str) - 1

        file_path = (fixtures_root / file_part).resolve()
        assert file_path.exists(), f"source_ref file does not exist: {file_path}"

        lines = file_path.read_text(encoding="utf-8").splitlines()
        assert 0 <= line_idx < len(lines), (
            f"source_ref line {lineno_str} is out of range for {file_part} "
            f"(file has {len(lines)} lines)"
        )
        assert "customer_id" in lines[line_idx], (
            f"Expected 'customer_id' at {ref}, got: {lines[line_idx]!r}"
        )

    def test_source_ref_is_inside_analytics_worker(self, event_result):
        """The source_ref must point into the analytics-worker directory."""
        analytics_ev = next(
            (e for e in event_result["evidence"] if e["subject"] == "analytics-worker"),
            None,
        )
        assert analytics_ev is not None, "No evidence for analytics-worker"
        assert "analytics-worker" in analytics_ev["source_ref"], (
            f"Expected source_ref to point inside analytics-worker, "
            f"got: {analytics_ev['source_ref']}"
        )


class TestEventRegressionRemoval:
    """
    REGRESSION TEST — Critical.

    Proves that removing event["customer_id"] from worker.py removes the
    dependency.  The real fixture is never mutated — we work on a tmp_path
    copy of the fixtures tree.
    """

    def test_removing_source_usage_removes_dependency(self, tmp_path, fixtures_root):
        """
        Copy the entire fixtures/ directory into tmp_path, then replace
        analytics-worker/worker.py with a version that does NOT access
        event["customer_id"].

        Running event_contract.run() against the modified fixtures root must
        return zero event-type dependencies — proving that the agent's
        detection is purely mechanism-driven (AST), not hardcoded.
        """
        # ── 1. Copy fixtures tree into tmp_path ──────────────────────────────
        modified_root = tmp_path / "fixtures"
        shutil.copytree(fixtures_root, modified_root)

        worker_path = modified_root / "analytics-worker" / "worker.py"
        assert worker_path.exists(), "worker.py not found in tmp fixture copy"

        # ── 2. Read original source and confirm it currently has the pattern ─
        original_source = worker_path.read_text(encoding="utf-8")
        assert 'event["customer_id"]' in original_source, (
            "Pre-condition failed: worker.py does not contain "
            "event[\"customer_id\"] before the removal test"
        )

        # ── 3. Write a version WITHOUT the event["customer_id"] access ───────
        modified_source = original_source.replace(
            'event["customer_id"]',
            '"REMOVED"  # customer_id reference intentionally deleted for regression test',
        )
        worker_path.write_text(modified_source, encoding="utf-8")

        # ── 4. Run event discovery against the modified fixtures root ─────────
        data = {
            "change_id": "regression-removal-001",
            "fixtures_root": str(modified_root),
            "old_field": "customer_id",
        }
        result = event_contract.run(data)

        # ── 5. Assert: zero event-type dependencies ────────────────────────────
        event_deps = [
            d for d in result["dependencies"] if d["edge_type"] == "event"
        ]
        assert not event_deps, (
            f"Regression failure: removing event[\"customer_id\"] from "
            f"worker.py should produce zero event dependencies, "
            f"but found: {[d['to_component'] for d in event_deps]}"
        )

    def test_original_fixture_is_not_mutated(self, fixtures_root):
        """
        After the regression test above, the real worker.py must still
        contain the original event["customer_id"] reference.
        """
        worker_path = fixtures_root / "analytics-worker" / "worker.py"
        source = worker_path.read_text(encoding="utf-8")
        assert 'event["customer_id"]' in source, (
            "Real worker.py was mutated! The regression test must only "
            "operate on a tmp_path copy."
        )


class TestEventResultShape:
    def test_result_validates_as_discovery_result(self, event_result):
        """Output must parse cleanly as DiscoveryResult."""
        validated = DiscoveryResult(**event_result)
        assert validated.change_id == "test-discovery-001"

    def test_evidence_has_detection_method(self, event_result):
        """Evidence content must record how the dependency was detected."""
        for ev in event_result["evidence"]:
            assert "detection_method" in ev["content"], (
                f"Evidence for {ev['subject']} is missing 'detection_method'"
            )
