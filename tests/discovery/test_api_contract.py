"""
tests/discovery/test_api_contract.py
=======================================
Tests for agents/discovery/api_contract.py

Proves:
  1. api-contract discovers checkout as an API consumer.
  2. api-contract discovers fraud as an API consumer.
  3. analytics-worker is NOT classified as an API consumer.
  4. Result validates as DiscoveryResult.
  5. Source references cite real file paths and real line numbers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.discovery import api_contract
from orchestrator.schemas import DiscoveryResult


@pytest.fixture(scope="module")
def api_result(base_data):
    return api_contract.run(base_data)


@pytest.fixture(scope="module")
def api_dependencies(api_result):
    return api_result["dependencies"]


class TestAPIConsumersFound:
    def test_finds_checkout_as_api_consumer(self, api_dependencies):
        """checkout must appear as an API consumer of account-service.
        Canonical direction: from_component=account-service, to_component=checkout."""
        checkout_edges = [
            d for d in api_dependencies
            if d["to_component"] == "checkout" and d["edge_type"] == "api"
        ]
        assert checkout_edges, (
            "Expected a dependency edge account-service -> checkout with edge_type='api'"
        )

    def test_finds_fraud_as_api_consumer(self, api_dependencies):
        """fraud must appear as an API consumer of account-service.
        Canonical direction: from_component=account-service, to_component=fraud."""
        fraud_edges = [
            d for d in api_dependencies
            if d["to_component"] == "fraud" and d["edge_type"] == "api"
        ]
        assert fraud_edges, (
            "Expected a dependency edge account-service -> fraud with edge_type='api'"
        )

    def test_both_consumers_point_to_account_service(self, api_dependencies):
        """Both API consumer edges must originate at account-service."""
        api_edges = [d for d in api_dependencies if d["edge_type"] == "api"]
        for edge in api_edges:
            assert edge["from_component"] == "account-service", (
                f"Expected from_component='account-service', got {edge['from_component']}"
            )


class TestAnalyticsWorkerExcluded:
    def test_analytics_worker_not_api_consumer(self, api_dependencies):
        """
        analytics-worker must NOT appear as an API consumer.
        Its dependency is discovered via event inspection, not API contract.
        """
        analytics_api_edges = [
            d for d in api_dependencies
            if d["from_component"] == "analytics-worker" and d["edge_type"] == "api"
        ]
        assert not analytics_api_edges, (
            "analytics-worker should not be classified as an API consumer; "
            "it is an event consumer (detected separately)"
        )


class TestAPIResultShape:
    def test_result_validates_as_discovery_result(self, api_result):
        """Output must parse cleanly as DiscoveryResult."""
        validated = DiscoveryResult(**api_result)
        assert validated.change_id == "test-discovery-001"

    def test_evidence_non_empty(self, api_result):
        assert len(api_result["evidence"]) >= 2  # at least openapi + one consumer


class TestAPISourceRefs:
    def test_source_refs_point_to_real_files(self, api_result, fixtures_root):
        """Every source_ref must resolve to an existing file."""
        for ev in api_result["evidence"]:
            file_part = ev["source_ref"].split(":")[0]
            resolved = (fixtures_root / file_part).resolve()
            assert resolved.exists(), (
                f"source_ref '{ev['source_ref']}' points to non-existent path"
            )

    def test_checkout_source_ref_contains_customer_id(self, api_result, fixtures_root):
        """The checkout evidence source_ref must cite a line with customer_id."""
        checkout_ev = next(
            (e for e in api_result["evidence"] if e["subject"] == "checkout"),
            None,
        )
        assert checkout_ev is not None, "No evidence found for checkout"
        ref = checkout_ev["source_ref"]
        file_part, _, lineno_str = ref.rpartition(":")
        line_idx = int(lineno_str) - 1
        file_path = (fixtures_root / file_part).resolve()
        line = file_path.read_text(encoding="utf-8").splitlines()[line_idx]
        assert "customer_id" in line, (
            f"Expected 'customer_id' at {ref}, got: {line!r}"
        )

    def test_fraud_source_ref_contains_customer_id(self, api_result, fixtures_root):
        """The fraud evidence source_ref must cite a line with customer_id."""
        fraud_ev = next(
            (e for e in api_result["evidence"] if e["subject"] == "fraud"),
            None,
        )
        assert fraud_ev is not None, "No evidence found for fraud"
        ref = fraud_ev["source_ref"]
        file_part, _, lineno_str = ref.rpartition(":")
        line_idx = int(lineno_str) - 1
        file_path = (fixtures_root / file_part).resolve()
        line = file_path.read_text(encoding="utf-8").splitlines()[line_idx]
        assert "customer_id" in line, (
            f"Expected 'customer_id' at {ref}, got: {line!r}"
        )
