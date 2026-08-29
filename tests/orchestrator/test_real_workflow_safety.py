"""Regression coverage for real-agent workspace isolation and fail-closed rehearsal."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

import orchestrator.agent_runner as agent_runner
import orchestrator.ledger as ledger
from agents.verification import coexistence_rehearsal
from agents.verification import critic
from orchestrator.main import app


def test_real_workflow_is_isolated_and_docker_failure_is_resumable(
    tmp_path: Path,
    monkeypatch,
):
    fixtures_root = Path(__file__).resolve().parents[2] / "fixtures"
    protected_files = {
        path: path.read_bytes()
        for path in fixtures_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }

    monkeypatch.setattr(agent_runner, "STUB_MODE", False)
    monkeypatch.setattr(agent_runner, "_WORKSPACE_BASE", tmp_path / "workspaces")

    with TestClient(app, raise_server_exceptions=True) as client:
        app.state.conn.close()
        conn = ledger.init_db(":memory:")
        app.state.conn = conn

        created = client.post(
            "/change-requests",
            json={"description": "customer_id -> account_id"},
        ).json()
        change_id = created["id"]

        with patch.object(
            coexistence_rehearsal,
            "_run_compose",
            side_effect=FileNotFoundError("docker unavailable"),
        ):
            response = client.post(
                f"/change-requests/{change_id}/approve",
                json={"gate": "coordinate", "approved_by": "integration-test"},
            )

        assert response.status_code == 200
        assert response.json()["new_status"] == "MODIFY"

        current = client.get(f"/change-requests/{change_id}").json()
        assert current["status"] == "REHEARSE"
        assert current["retry_count"] == 1

        evidence = client.get(f"/change-requests/{change_id}/evidence").json()["evidence"]
        rehearsal = [
            item for item in evidence
            if item["claim_type"] == "test_result" and item["subject"] == "coexistence"
        ][-1]
        assert rehearsal["content"]["returncode"] == 127
        assert rehearsal["confidence"] == "refuted"
        assert any(item["subject"] == "workflow-error" for item in evidence)

        workspace = tmp_path / "workspaces" / change_id / "fixtures"
        schema = (workspace / "platform-config" / "schema.sql").read_text(encoding="utf-8")
        test_source = (
            workspace
            / "platform-config"
            / "tests"
            / "test_account_id_migration.py"
        ).read_text(encoding="utf-8")
        assert "account_id" in schema
        assert '"customer_id" not in' in test_source
        assert '"account_id" not in' not in test_source

        # Once the missing prerequisite is restored, the additive resume API
        # reruns the rehearsal, executes contract tests in the same workspace,
        # and reaches the human legacy-removal gate.
        with (
            patch.object(coexistence_rehearsal, "_run_compose", return_value=(0, "ok")),
            patch.object(
                critic,
                "_get_evidence",
                side_effect=lambda *_args, **_kwargs: {
                    "evidence": ledger.get_evidence(conn, change_id)
                },
            ),
        ):
            resumed = client.post(f"/change-requests/{change_id}/resume")

        assert resumed.status_code == 200
        current = client.get(f"/change-requests/{change_id}").json()
        assert current["status"] == "APPROVE"
        assert current["retry_count"] == 0
        gate = client.get(f"/change-requests/{change_id}/gate").json()
        assert gate["decided"] is True
        assert gate["result"] == "VERIFIED"

        for repo in workspace.iterdir():
            if not (repo / ".git").exists():
                continue
            tracked = subprocess.run(
                ["git", "-C", str(repo), "ls-files"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            assert not any("__pycache__" in path or path.endswith(".pyc") for path in tracked)

        conn.close()

    for path, before in protected_files.items():
        assert path.read_bytes() == before, f"workflow mutated repository fixture: {path}"
