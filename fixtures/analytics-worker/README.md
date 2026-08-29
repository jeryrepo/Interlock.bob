# analytics-worker fixture

Undocumented event consumer used to prove source-based discovery. The worker
directly reads `event["customer_id"]`; no published API contract declares this
dependency. The graph therefore records an `event` edge whose independent
documentation status is `undocumented`.
