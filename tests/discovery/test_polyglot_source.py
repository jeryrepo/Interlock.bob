"""
Tests for the polyglot-source-discovery agent.

The stakes: a consumer discovery cannot see is a consumer the gate never
requires, which is a false VERIFIED waiting to happen. These tests pin that
non-Python consumers are found, that naming-convention variants are found but
honestly marked as hypotheses, that vendored code is never scanned, and — the
end of the story — that a discovered JS consumer actually blocks the gate.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import orchestrator.gate as gate
import orchestrator.ledger as ledger
from agents.discovery import polyglot_source
from orchestrator.agent_registry import AGENT_REGISTRY, POLYGLOT_DISCOVERY
from orchestrator.schemas import DiscoveryResult

CHANGE_ID = "chg-polyglot"


def _run(root: Path) -> dict:
    return polyglot_source.run({
        "change_id": CHANGE_ID,
        "components_root": str(root),
        "provider": "account-service",
        "old_field": "customer_id",
        "new_field": "account_id",
    })


def _edges(result: dict) -> dict[str, dict]:
    return {d["to_component"]: d for d in result["dependencies"]}


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-language detection
# ---------------------------------------------------------------------------

class TestLanguages:
    def test_javascript_property_access_and_quoted_key(self, tmp_path):
        _write(tmp_path, "web-checkout/src/cart.js", """\
            export function total(account) {
              const id = account.customer_id;
              return { "customer_id": id };
            }
        """)
        result = _run(tmp_path)
        edge = _edges(result)["web-checkout"]
        assert edge["from_component"] == "account-service"
        assert edge["edge_type"] == "undocumented"

    def test_typescript_interface_and_destructuring(self, tmp_path):
        _write(tmp_path, "web-portal/models.ts", """\
            interface Account { customer_id: string; }
            const { customer_id } = payload;
        """)
        assert "web-portal" in _edges(_run(tmp_path))

    def test_java_camel_case_and_json_property(self, tmp_path):
        _write(tmp_path, "fraud-java/src/Account.java", """\
            public class Account {
                @JsonProperty("customer_id")
                private String customerId;
                public String getCustomerId() { return customerId; }
            }
        """)
        result = _run(tmp_path)
        assert "fraud-java" in _edges(result)
        ev = next(e for e in result["evidence"] if e["subject"] == "fraud-java")
        matched = {r["matched"] for r in ev["content"]["refs"]}
        assert "customer_id" in matched     # the annotation, wire name
        assert "customerId" in matched      # the field, convention variant

    def test_go_struct_tag_and_pascal_field(self, tmp_path):
        _write(tmp_path, "billing-go/main.go", """\
            type Account struct {
                CustomerId string `json:"customer_id"`
            }
        """)
        assert "billing-go" in _edges(_run(tmp_path))

    def test_ruby_symbol_and_string_key(self, tmp_path):
        _write(tmp_path, "notify-rb/app.rb", """\
            id = payload[:customer_id]
            other = payload["customer_id"]
        """)
        assert "notify-rb" in _edges(_run(tmp_path))

    def test_camel_only_matches_are_hypotheses(self, tmp_path):
        """
        A convention variant is an inference, not an observation. A component
        seen ONLY through `customerId` must say so — a human may refute it.
        """
        _write(tmp_path, "maybe-java/src/A.java", """\
            String x = record.customerId;
        """)
        result = _run(tmp_path)
        ev = next(e for e in result["evidence"] if e["subject"] == "maybe-java")
        assert ev["confidence"] == "hypothesis"

    def test_wire_name_match_makes_the_component_confirmed(self, tmp_path):
        _write(tmp_path, "sure-java/src/A.java", """\
            String id = json.getString("customer_id");
        """)
        result = _run(tmp_path)
        ev = next(e for e in result["evidence"] if e["subject"] == "sure-java")
        assert ev["confidence"] == "confirmed"

    def test_a_migrated_consumer_is_still_discovered_via_new_field(self, tmp_path):
        """External mode: work already done, old symbol gone — still a consumer."""
        _write(tmp_path, "done-ts/api.ts", """\
            const id = account["account_id"];
        """)
        assert "done-ts" in _edges(_run(tmp_path))

    def test_function_symbols_get_camel_variants_too(self, tmp_path):
        """A JS port of a webhook consumer calls deliverViaWebhook."""
        result = polyglot_source.run({
            "change_id": CHANGE_ID,
            "components_root": str(tmp_path),
            "provider": "event-publisher",
            "old_field": "deliver_via_webhook",
            "new_field": "deliver_via_pubsub",
        })
        assert result["dependencies"] == []  # empty root, no crash

        _write(tmp_path, "notify-js/hook.js", """\
            import { deliverViaWebhook } from "./transport";
            deliverViaWebhook(event);
        """)
        result = polyglot_source.run({
            "change_id": CHANGE_ID,
            "components_root": str(tmp_path),
            "provider": "event-publisher",
            "old_field": "deliver_via_webhook",
            "new_field": "deliver_via_pubsub",
        })
        assert "notify-js" in {d["to_component"] for d in result["dependencies"]}


# ---------------------------------------------------------------------------
# What must NOT be found
# ---------------------------------------------------------------------------

class TestNoise:
    def test_longer_identifiers_do_not_match(self, tmp_path):
        _write(tmp_path, "web-x/a.js", """\
            const customer_id_extra = 1;
            const my_customer_id = 2;
        """)
        assert _run(tmp_path)["dependencies"] == []

    def test_node_modules_is_never_scanned(self, tmp_path):
        _write(tmp_path, "web-x/node_modules/somelib/index.js", """\
            exports.customer_id = "vendored";
        """)
        _write(tmp_path, "web-x/src/app.js", "export const nothing = 1;\n")
        assert _run(tmp_path)["dependencies"] == []

    def test_build_output_is_never_scanned(self, tmp_path):
        _write(tmp_path, "svc-java/target/Gen.java", "String customerId;\n")
        _write(tmp_path, "svc-java/src/Main.java", "class Main {}\n")
        assert _run(tmp_path)["dependencies"] == []

    def test_minified_bundles_are_skipped(self, tmp_path):
        _write(tmp_path, "web-x/static/app.min.js", 'x.customer_id;\n')
        assert _run(tmp_path)["dependencies"] == []

    def test_python_is_left_to_the_ast_agents(self, tmp_path):
        _write(tmp_path, "checkout/checkout.py", 'x = r["customer_id"]\n')
        assert _run(tmp_path)["dependencies"] == []

    def test_the_provider_is_not_its_own_consumer(self, tmp_path):
        _write(tmp_path, "account-service/client.ts", 'a["customer_id"];\n')
        result = _run(tmp_path)
        assert result["dependencies"] == []
        # But the evidence still records what the provider references.
        assert any(e["subject"] == "account-service" for e in result["evidence"])


# ---------------------------------------------------------------------------
# Contract and wiring
# ---------------------------------------------------------------------------

class TestContract:
    def test_output_validates_as_discovery_result(self, tmp_path):
        _write(tmp_path, "web-x/a.ts", 'p["customer_id"];\n')
        DiscoveryResult(**_run(tmp_path))

    def test_registered_for_every_change_kind(self):
        for kind in ("field_rename", "api_contract_change", "transport_migration"):
            assert POLYGLOT_DISCOVERY in AGENT_REGISTRY[(kind, "DISCOVERY")], kind


# ---------------------------------------------------------------------------
# The end of the story: a discovered JS consumer blocks the gate
# ---------------------------------------------------------------------------

class TestGateIntegration:
    def test_a_javascript_consumer_blocks_the_gate_until_proven(self, tmp_path):
        """
        The whole point of discovering it. Feed the polyglot edges into the
        ledger exactly as _discovery() does, verify every OTHER requirement,
        and the gate must still refuse — naming the JS consumer — because
        nothing has proven its migration.

        Before this agent existed the same repo produced VERIFIED: the JS
        consumer got no edge, so the gate never asked about it.
        """
        _write(tmp_path, "web-checkout/src/cart.js", """\
            const id = account.customer_id;
        """)
        result = _run(tmp_path)

        conn = ledger.init_db(":memory:")
        ledger.create_change(conn, "c1", "rename")
        ledger.set_change_spec(conn, "c1", "field_rename", {
            "kind": "field_rename", "provider": "account-service",
            "components_root": str(tmp_path),
            "old_field": "customer_id", "new_field": "account_id",
        })
        for d in result["dependencies"]:
            ledger.add_dependency(
                conn, "c1", d["from_component"], d["to_component"],
                d["edge_type"], d["reason"],
            )
        # Everything except the JS consumer is proven.
        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", "provider_patch"
        )
        ledger.upsert_work_item(
            conn, "c1", "account-service", "verified", gate.REHEARSAL_STEP_KIND
        )

        decision = gate.evaluate_gate(conn, "c1")
        assert decision.result == "NOT_PROVEN_SAFE"
        assert "web-checkout" in decision.unresolved
