"""
orchestrator/schemas/change_spec.py
====================================
Structured description of *what* is being changed.

Before this existed the change was an unparsed string: `CreateChangeRequest` was
`description: str`, nothing downstream could branch on it, and the real agents —
which need `old_field` / `new_field` / `provider` — could not be driven from an
HTTP request at all.

The spec supplies **nouns only**: which component owns the change, where the
components live, and which symbols are moving. It carries no policy, no
thresholds, and no required-evidence set. That separation is deliberate and is
recorded in ADR-0002: the spec arrives in a request body, so anything the gate
read from it as *policy* could be weakened by whoever writes the spec.

See docs/adr/0003-change-kind-discriminator.md.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class _SpecBase(BaseModel):
    """Fields every change kind needs, whatever its surface."""

    provider: str
    """Component that owns the thing being changed. Replaces gate.PROVIDER."""

    components_root: str = "fixtures"
    """Directory whose immediate subdirectories are the components."""

    notes: str | None = None

    implementation: Literal["builtin", "external"] = "builtin"
    """
    Who performs the code change.

    ``builtin``  — Interlock's own rewriters edit the source. Python only, and
                   only for the shapes those agents recognise.
    ``external`` — a human or another coding agent (IBM Bob) already did the
                   work; Interlock verifies the symbols moved, the component's
                   own tests pass, and a real commit exists. Language-agnostic,
                   and the only workable mode for transitions no rewriter can
                   perform — a C-to-Python port, a framework swap, a rewrite.

    This is a noun about the change, not gate policy: the gate requires exactly
    the same proof either way. See ADR-0002.
    """


class FieldRenameSpec(_SpecBase):
    """A schema or model field rename, e.g. customer_id -> account_id."""

    kind: Literal["field_rename"] = "field_rename"
    old_field: str
    new_field: str


class ApiContractChangeSpec(_SpecBase):
    """
    A change to a published API contract.

    Shares the shape of FieldRenameSpec, and that near-duplication is
    load-bearing rather than accidental: the agent registry and the gate's
    required step kinds both key off `kind`, and they must stay independently
    changeable. Two small classes beat one class with a mode flag.
    """

    kind: Literal["api_contract_change"] = "api_contract_change"
    old_field: str
    new_field: str
    endpoint: str | None = None


class TransportMigrationSpec(_SpecBase):
    """
    Moving event delivery from webhooks to pub/sub.

    `old_symbol` / `new_symbol` are what the consumer-side switch looks like in
    source, so the existing well-tested migration agent can perform it. Proving
    the retired webhook has drained is a separate work item — see
    `gate._REQUIRED_STEP_KINDS`.
    """

    kind: Literal["transport_migration"] = "transport_migration"
    topic: str
    webhook_path: str
    old_symbol: str
    new_symbol: str


ChangeSpec = Annotated[
    Union[FieldRenameSpec, ApiContractChangeSpec, TransportMigrationSpec],
    Field(discriminator="kind"),
]

CHANGE_KINDS: tuple[str, ...] = (
    "field_rename",
    "api_contract_change",
    "transport_migration",
)


def symbols_for(spec: dict) -> tuple[str, str]:
    """
    Return the (old, new) identifier pair for any change kind.

    Field renames and API contract changes move a field; a transport migration
    moves a delivery symbol. Callers that only need "what is being replaced"
    should use this rather than branching on kind themselves.
    """
    if spec.get("kind") == "transport_migration":
        return spec["old_symbol"], spec["new_symbol"]
    return spec["old_field"], spec["new_field"]
