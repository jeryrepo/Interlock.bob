"""
frontend/utils/spec.py
=======================
Builds the ChangeSpec payload the UI posts to `POST /change-requests`.

Lives here rather than in `streamlit_app.py` for the same reason `derive.py`
does: a Streamlit app module executes top to bottom on import, so anything
defined inside it cannot be unit-tested in isolation. Pure functions belong in
`utils/`.

This assembles *shape* only. The backend validates the payload against the real
`ChangeSpec` discriminated union and rejects anything malformed — the UI does
not duplicate that validation, and must not, or the two will drift.
"""

from __future__ import annotations

CHANGE_KINDS: tuple[str, ...] = (
    "field_rename",
    "api_contract_change",
    "transport_migration",
)


def build_spec(
    kind: str,
    provider: str,
    old: str,
    new: str,
    components_root: str = "fixtures",
) -> dict:
    """
    Assemble a ChangeSpec payload.

    `old` and `new` land on different keys depending on the kind: a field rename
    and an API contract change move a *field*, while a transport migration moves
    a *delivery symbol*. The backend's `symbols_for()` reads them back the same
    way.
    """
    spec: dict = {
        "kind": kind,
        "provider": provider,
        "components_root": components_root or "fixtures",
    }
    if kind == "transport_migration":
        spec.update(
            {
                "topic": f"{provider}.events",
                "webhook_path": f"/hooks/{provider}",
                "old_symbol": old,
                "new_symbol": new,
            }
        )
    else:
        spec.update({"old_field": old, "new_field": new})
    return spec


def missing_fields(provider: str, old: str, new: str) -> list[str]:
    """
    Which required inputs are blank.

    Used to validate on click. A Streamlit widget's `disabled=` flag is computed
    from the *previous* run, so gating the submit button on these values would
    leave it dead until an unrelated rerun.
    """
    return [
        label
        for label, value in (("provider", provider), ("from", old), ("to", new))
        if not str(value).strip()
    ]
