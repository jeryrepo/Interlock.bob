"""
Coexistence probe — the assertions that constitute the proof.

The property under test: a single running provider serves BOTH the legacy and
the new field shape during the migration window. That is what makes it possible
to cut consumers over one at a time instead of in a coordinated big-bang deploy.

``check_payload()`` is a pure function so there is exactly one implementation of
"what coexistence means", shared by both ways of running the rehearsal:

- ``coexistence_rehearsal.py`` starts the provider as a local uvicorn subprocess
  and calls ``check_payload`` directly. This is the default — it needs no daemon,
  runs in seconds, and works in CI.
- ``docker compose run --rm coexistence-probe`` executes ``main()`` below inside
  a container, for demo purposes.

Deliberately not under ``fixtures/``: the discovery agents treat every immediate
subdirectory of the fixtures root as a component, so a probe living there would
appear in the dependency graph as a phantom consumer.

Container-mode configuration (environment):
    PROVIDER_URL  base URL of the live provider           (default http://account-service:8000)
    OLD_FIELD     legacy field that must still be served  (default customer_id)
    NEW_FIELD     new field introduced by the migration   (default account_id)
    EXPECT_NEW    "1" once the provider has been patched  (default "0")

Exit code 0 means coexistence holds. Anything else means it does not.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# Standard library only. The probe must not depend on anything it is testing.

PROBE_ACCOUNT_KEY = "probe-001"


# ---------------------------------------------------------------------------
# The proof — pure, shared by both run modes
# ---------------------------------------------------------------------------

def check_payload(
    payload: dict,
    old_field: str,
    new_field: str,
    expect_new: bool,
) -> list[str]:
    """
    Return a list of human-readable failures. Empty means coexistence holds.

    Three conditions, and the third is the one people forget:

    1. The legacy field must still be served, or un-migrated consumers break.
    2. Once the provider is patched, the new field must be served alongside it,
       or migrated consumers break.
    3. *Before* the patch, the new field must be absent. Without this the
       rehearsal would pass whether or not it was observing the state it claims
       to be observing, which would make the whole exercise decorative.
    """
    failures: list[str] = []

    if old_field not in payload:
        failures.append(
            f"legacy field {old_field!r} absent — un-migrated consumers would break"
        )

    if expect_new and new_field not in payload:
        failures.append(
            f"new field {new_field!r} absent — migrated consumers would break"
        )

    if not expect_new and new_field in payload:
        failures.append(
            f"new field {new_field!r} present before the provider patch — "
            "the rehearsal is not observing the pre-migration state"
        )

    return failures


def describe(old_field: str, new_field: str, expect_new: bool) -> str:
    """One-line summary of what a passing run proved."""
    if expect_new:
        return f"provider serves both {old_field!r} and {new_field!r}"
    return f"provider serves {old_field!r} only (pre-migration)"


# ---------------------------------------------------------------------------
# Container entry point
# ---------------------------------------------------------------------------

_HEALTH_ATTEMPTS = 30
_HEALTH_DELAY_SECONDS = 1.0


def _get(base_url: str, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    base_url = os.environ.get(
        "PROVIDER_URL", "http://account-service:8000"
    ).rstrip("/")
    old_field = os.environ.get("OLD_FIELD", "customer_id")
    new_field = os.environ.get("NEW_FIELD", "account_id")
    expect_new = os.environ.get("EXPECT_NEW", "0") == "1"

    for attempt in range(1, _HEALTH_ATTEMPTS + 1):
        try:
            _get(base_url, "/health", timeout=2.0)
            print(f"provider healthy after {attempt} attempt(s)")
            break
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(_HEALTH_DELAY_SECONDS)
    else:
        print(f"FAIL: provider never became healthy at {base_url}", file=sys.stderr)
        return 2

    payload = _get(base_url, f"/accounts/{PROBE_ACCOUNT_KEY}")
    print(f"provider response: {payload}")

    failures = check_payload(payload, old_field, new_field, expect_new)
    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(f"OK: coexistence holds — {describe(old_field, new_field, expect_new)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
