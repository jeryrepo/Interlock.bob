# Architecture Decision Records

Each file records one architectural decision: why it was made, what it cost, and
what would reopen it. Newest decisions supersede older ones by reference — an
accepted ADR is never edited to say something different, it is superseded.

**If you are an AI coding agent working in this repository, read this index
before changing architecture.** Do not contradict an accepted ADR. If you believe
one is wrong, write a new ADR that supersedes it and say so explicitly in both
files.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-exit-48h-mvp-scope.md) | Exit the 48-hour MVP scope | Accepted |
| [0002](0002-deterministic-gate-stays-single-function.md) | The deterministic gate stays a single function | Accepted |
| [0003](0003-change-kind-discriminator.md) | Introduce a ChangeSpec kind discriminator | Accepted |
| [0004](0004-streamlit-retained.md) | Keep Streamlit; defer React | Accepted |
| [0005](0005-cli-and-mcp-as-agent-surface.md) | CLI and MCP server as the agent-facing surface | Accepted |
| [0006](0006-rehearsal-uses-a-subprocess-not-docker.md) | Coexistence rehearsal uses a subprocess, not Docker | Accepted |

New ADRs start from [0000-template.md](0000-template.md).
