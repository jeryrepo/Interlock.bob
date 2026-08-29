# account-service fixture

Provider fixture for the `customer_id -> account_id` demo. The canonical copy is
pre-migration and exposes `customer_id`; the provider-patch agent adds
`account_id` while retaining the legacy field in an isolated workspace.

Run its baseline tests with `python -m pytest -q` from this directory.
