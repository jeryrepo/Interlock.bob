# ADR-0005: CLI and MCP server as the agent-facing surface

- **Status:** Accepted
- **Date:** 2026-08-29

## Context

Interlock needs to be usable at pull-request time and "easily pulled through IBM
Bob or any other agent". Today it has neither: no CLI, no `pyproject.toml`, no
`[project.scripts]`, not a single `if __name__ == "__main__"` block, and no
`.github/`. The only entry points are `uvicorn` and `streamlit run`.

A control plane that can only be driven by starting two servers and clicking
through a browser cannot participate in a developer's PR workflow.

## Decision

Build three surfaces, in order, each wrapping the previous:

1. **A local Typer CLI** (`interlock discover | plan | verify | gate | status`),
   packaged via `pyproject.toml`. `interlock gate` exits non-zero on
   `NOT_PROVEN_SAFE` — that exit code is what makes it usable in a pre-push hook
   or a CI step. The agent `run(data, ...)` functions are already pure
   dict-in/dict-out and independently tested, so they are the natural bodies.
2. **An MCP server** exposing the same verbs as tools, shipped with `.bob/mcp.json`
   for IBM Bob and `.mcp.json` for Claude Code, Cursor, and Copilot.
3. **A GitHub Action** on `pull_request` that runs `interlock gate` and posts the
   verdict and unresolved consumers as a PR comment.

MCP is the specific mechanism for the pull-through goal: Bob reads `AGENTS.md`
natively and loads MCP servers from `.bob/mcp.json` (project) or `~/.bob/mcp.json`
(global). The same server serves every other major coding agent unchanged.

The CLI and the MCP tools must call the same `evaluate_gate()` as the API. None
of them may re-derive or cache a verdict — see ADR-0002.

## Consequences

Turns Interlock from a demo into something a developer runs, and from a
standalone app into a tool other agents can compose with. One more integration
contract to keep working, and the GitHub Action is the piece that most clearly
exits the original MVP scope — hence ADR-0001.

## Revisit when

MCP is superseded as the agent tool-calling standard, or Orchestrate's A2A path
(ADR pending) makes the stdio server redundant for the IBM deployment.
