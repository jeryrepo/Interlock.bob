# ADR-0002: The deterministic gate stays a single function

- **Status:** Accepted
- **Date:** 2026-08-29

## Context

`AGENTS.md` invariant 1 states that the gate is deterministic and lives in
exactly one place: `orchestrator/gate.py::evaluate_gate()`, pure read-only Python
with zero LLM involvement, which no critic agent, frontend, or other component
may compute, duplicate, cache, or override.

Generalizing to multiple change kinds puts that invariant under direct pressure,
because kinds have different proof conditions. A renamed field is safe when every
consumer is migrated and green. A webhook-to-pub/sub cut-over is safe when every
subscriber consumes from the topic **and** the retired webhook has seen no
traffic for a quiet period.

Two tempting designs, both rejected:

- **A dispatch table** — `GATE_POLICIES[kind] -> Callable`. Note the obvious
  defence of it is actually correct: a dict of pure predicates defined inside
  `gate.py` does literally live in one file, and is still pure and deterministic.
  The real objections are different. Reviewability collapses: today you read
  ~40 lines and know what "safe" means for the whole system; with N predicates
  you must read N and hold the union in your head. Blast radius grows with
  product surface, so the file with the highest review cost becomes the file
  with the highest churn. And failure is asymmetric — a predicate can be
  accidentally permissive in ways data cannot (`return GateDecision(result=
  "VERIFIED", ...)` is one plausible line).
- **A `required_evidence` policy derived from the change spec** — this one is
  worse, and the reason is a security property, not an aesthetic one. The spec
  arrives in the request body. If gate policy is derived from spec *contents*,
  then whoever writes the spec — a client today, an LLM intake agent later — can
  weaken the gate from the outside. Today `evaluate_gate()` reads only
  `dependency_edge` and `consumer_migration`, both orchestrator-written.

## Decision

`evaluate_gate()` remains one function, and its **predicate does not change at
all**. Per-kind variation is expressed solely as the *required work-item set*:

> For every `(component, step_kind)` pair required by this change, a `work_item`
> row exists with `status = 'verified'`.

```python
# Policy, owned by the gate. Data, not callables. One screen, reviewable.
_REQUIRED_STEP_KINDS: dict[str, tuple[str, ...]] = {
    "field_rename":        ("migrate",),
    "api_contract_change": ("migrate",),
    "transport_migration": ("subscribe", "webhook_quiet"),
}
_DEFAULT_STEP_KINDS = ("migrate",)
```

The transport condition decomposes exactly into those two work items. Proving
quiescence is an *agent's* job: a verification agent measures the quiet window
and the orchestrator marks the `webhook_quiet` item verified. The gate never
learns what quiescence means, never parses evidence JSON, and never grows a
threshold.

Three separated concerns, and the separation is the point:

| Source | Supplies | Example |
| --- | --- | --- |
| The change spec | **nouns** | `provider`, `topic` |
| `gate.py` | **policy** | `_REQUIRED_STEP_KINDS`, any threshold floor |
| The ledger | **facts** | `dependency_edge`, `work_item` rows |

`spec.provider` is a noun and is safe for the gate to read. A hypothetical
`spec.quiet_period_seconds` is a threshold and must not be — if such a knob is
ever needed it lives in `gate.py`, and a spec may only raise it, never lower it.

Unknown kinds resolve via `.get(kind, _DEFAULT_STEP_KINDS)` — the strictest known
set, never an empty one. This fails closed; a dispatch table's natural failure is
`KeyError` or a permissive fallthrough.

## Consequences

Stronger than merely centralising policy: the gate's *logic* is unchanged, not
just co-located. Output stays byte-identical for `field_rename` and for legacy
description-only changes, so the existing gate tests pass untouched.

The cost is an expressiveness ceiling. Some future kind may not decompose into a
set of per-component work items without contortion. That discomfort is the
signal, not a bug to route around.

## Revisit when

A change kind genuinely cannot be expressed as a required set of
`(component, step_kind)` pairs. The correct response is a new ADR superseding
this one — never a lambda smuggled into the requirement set, and never a policy
field added to the request body.
