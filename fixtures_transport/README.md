# fixtures_transport

Component tree for the **webhook -> pub/sub** change kind
(`transport_migration`), kept separate from `fixtures/` because discovery
treats every immediate subdirectory of a components root as a component.

| Component | Role |
| --- | --- |
| `event-publisher/` | provider; owns the delivery transport |
| `notifier/` | documented subscriber |
| `audit-sink/` | **undocumented** subscriber, found only by reading source |

Each subscriber carries a `webhook_activity.json` recording how much traffic
it still sends to the retired webhook path. The `webhook-quiet` verification
agent reads it, and refuses to call a subscriber drained while its source
still references the webhook symbol.

Run against it with:

```bash
interlock check --kind transport_migration \
  --old deliver_via_webhook --new deliver_via_pubsub \
  --provider event-publisher --components-root fixtures_transport
```
