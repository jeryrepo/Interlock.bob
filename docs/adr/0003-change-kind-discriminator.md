# ADR-0003: Introduce a ChangeSpec kind discriminator

- **Status:** Accepted
- **Date:** 2026-08-29

## Context

`customer_id -> account_id` is one instance of a class of critical operations,
not the scope of the product. Interlock must also cover API contract changes and
webhook-to-pub/sub transport migrations.

Today the change is an unparsed string. `CreateChangeRequest`
(`orchestrator/main.py`) is `description: str`; the `change_request` table has no
`kind`, `provider`, or field columns; and `body.description` is stored verbatim
and never reaches an agent. There is no change-kind discriminator anywhere in the
codebase, so nothing downstream can branch on what is being changed.

Meanwhile the parts that look domain-specific mostly are not. The
`Dependency.edge_type` vocabulary — `api | event | db | undocumented` — already
spans all three kinds, and the discovery pod already ships one agent per kind.
The taxonomy was right from the start; the plumbing is what is single-purpose.

## Decision

Add `orchestrator/schemas/change_spec.py` defining a Pydantic discriminated
union on `kind`, with three members: `FieldRenameSpec`, `ApiContractChangeSpec`,
`TransportMigrationSpec`.

**Persist it in a new `change_spec` table, 1:1 with `change_request` — do not add
columns to `change_request`.** `ledger.init_db()` runs `executescript(schema.sql)`
on *every* startup, so a new `CREATE TABLE IF NOT EXISTS` applies itself to
existing databases automatically. New *columns* are the only thing that
silently does not apply. So the rule is: **add a table, never a column**, and no
migration runner is needed at all.

The spec supplies **nouns only** — `provider`, `components_root`, the symbols
being changed. It carries no policy knobs, no thresholds, and no
`required_evidence`. See ADR-0002 for why: the spec arrives in a request body,
and gate policy must never be client-supplied.

Routing rule: **spec present ⇒ real agent registry; spec absent ⇒ existing stub
workflow.** No existing test sends a spec, so every existing test keeps the stub
path and stays green — including `tests/orchestrator/test_agent_runner.py:81`,
which asserts `STUB_MODE is True`. Back-compat costs one `if`.

`gate.PROVIDER`, currently a module constant and blessed by `AGENTS.md` invariant
6 as "the one deliberate component constant", moves onto the spec.

`POST /change-requests` continues to accept `{description: str}` alone. The
structured spec arrives as additive optional fields, preserving invariant 7.

Because the orchestrator must hand real agents their `old_field`, `new_field`,
`provider`, and repo pointer, this lands alongside the un-stubbing work with
`field_rename` as the only registered kind — behaviour identical to today,
provable by the existing suite — and the other two kinds follow.

## Consequences

Unlocks every downstream goal: three change kinds, a CLI that accepts a change
description, and a watsonx surface that can map natural language onto a spec.

Costs almost nothing structurally, because the side-table choice sidesteps the
migration problem entirely rather than solving it. `tests/orchestrator/test_schema_ledger.py`
asserts `expected.issubset(tables)`, so new tables are explicitly safe.

Two real costs. Near-duplication: `FieldRenameSpec` and `ApiContractChangeSpec`
have nearly the same shape. That is deliberate — do not collapse them behind a
`surface` enum, because the agent registry and the required step kinds key off
`kind` and must stay independently changeable. Two five-line classes are cheaper
than one class with a mode flag.

And orphaned rows: existing `consumer_migration` rows in a developer's
`interlock.db` are not migrated to `work_item`. For this project the answer is to
delete the local dev database, documented in the README, rather than build
migration machinery to avoid it.

## Revisit when

A fourth change kind arrives that is not a provider-and-consumers shape at all.
