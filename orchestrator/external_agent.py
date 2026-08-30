"""
orchestrator/external_agent.py
===============================
An OpenAI-compatible `/chat/completions` surface, so watsonx Orchestrate can
register Interlock as an external agent.

This is the SECONDARY integration path. The primary one is the MCP server in
`interlock_mcp/`, which Orchestrate can consume directly as a toolkit — the
hackathon guide lists "MCP servers" among the supported ways to add a tool, and
reusing the server that already serves IBM Bob, Claude Code and Cursor beats
writing a second surface. This endpoint exists for the case where a
conversational agent is wanted instead of a tool, and because it costs little
once `interlock_cli.core` already does the work.

Safety
------
Two properties matter more than the protocol:

**The verdict is never generated.** `core.gate_status()` produces it, and this
module prints it verbatim. The optional watsonx.ai narration is appended as
prose beneath it and is stripped of gate vocabulary first. A model cannot
soften a NOT_PROVEN_SAFE result because no model is ever asked what the result
is.

**The endpoint refuses to serve unauthenticated.** It runs real test suites
against a component tree, so an open instance would be a remote code execution
surface. With no `INTERLOCK_EXTERNAL_AGENT_KEY` configured it returns 503 —
disabled — rather than defaulting to open.

Additive only: mounting this router changes no existing route, which keeps
`AGENTS.md` invariant 7.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from interlock_cli import core
from orchestrator import watsonx
from orchestrator.settings import Settings

router = APIRouter(tags=["watsonx-orchestrate"])

# Symbol extraction, in priority order. Order matters: a bare "X to Y" pattern
# applied to "is it safe to rename customer_id to account_id" matches
# "safe to rename" first and would run entirely the wrong change. Anchoring on
# an arrow or an explicit verb comes first, and the loose form requires both
# sides to look like code identifiers rather than English.
_RENAME_PATTERNS = (
    # customer_id -> account_id
    re.compile(r"([A-Za-z_][\w.]*)\s*(?:->|\u2192|=>)\s*([A-Za-z_][\w.]*)"),
    # rename / migrate / move / port / switch / replace X to|into|with Y
    re.compile(
        r"\b(?:rename|migrat\w*|mov\w*|port\w*|switch\w*|replac\w*)"
        r"\s+(?:the\s+|from\s+)?([A-Za-z_][\w.]*)"
        r"\s+(?:to|into|with|for)\s+([A-Za-z_][\w.]*)",
        re.I,
    ),
    # loose "X to Y", but only when both sides carry a _ or . and so look like
    # symbols rather than ordinary words
    re.compile(
        r"\b([A-Za-z_]\w*[_.]\w+)\s+(?:to|into)\s+([A-Za-z_]\w*[_.]\w+)\b", re.I
    ),
)
_PROVIDER = re.compile(
    r"\b(?:on|in|for|provider|service)\s+([A-Za-z0-9][\w.-]*)", re.I
)
_KIND = {
    "transport": "transport_migration",
    "webhook": "transport_migration",
    "pubsub": "transport_migration",
    "pub/sub": "transport_migration",
    "api": "api_contract_change",
    "contract": "api_contract_change",
}


class ChatMessage(BaseModel):
    role: str
    content: str | None = None


class ChatCompletionRequest(BaseModel):
    """
    The subset of the OpenAI chat-completions request Interlock uses.

    Deliberately permissive — Orchestrate sends fields this service does not
    need, and rejecting an unrecognised field would break on a platform update.
    """

    model: str | None = None
    messages: list[ChatMessage] = []
    stream: bool = False


# ---------------------------------------------------------------------------
# Intent extraction
# ---------------------------------------------------------------------------

def parse_intent(text: str, default_root: str) -> dict[str, Any] | None:
    """
    Pull a change spec out of a sentence, or return None.

    Regex, not a model, and that is the point: a model deciding what change to
    run would put a generated value on the path to a verdict. If the sentence is
    ambiguous the honest answer is to ask, which is what returning None causes.
    """
    rename = next(
        (m for pattern in _RENAME_PATTERNS for m in [pattern.search(text)] if m),
        None,
    )
    if not rename:
        return None
    old_symbol, new_symbol = rename.group(1), rename.group(2)

    provider_match = _PROVIDER.search(text)
    provider = provider_match.group(1) if provider_match else None
    if not provider:
        return None

    lowered = text.lower()
    kind = next(
        (k for word, k in _KIND.items() if word in lowered), "field_rename"
    )
    root_match = re.search(r"\b(?:root|components?[- ]root)\s+(\S+)", text, re.I)
    return {
        "kind": kind,
        "provider": provider,
        "old": old_symbol,
        "new": new_symbol,
        "components_root": root_match.group(1) if root_match else default_root,
    }


_HELP = (
    "I check whether a breaking change is safe to ship across your services.\n\n"
    "Tell me the change like this:\n"
    "  • `is it safe to rename customer_id to account_id on account-service?`\n"
    "  • `migrate deliver_via_webhook to deliver_via_pubsub in event-publisher`\n\n"
    "I discover every consumer — including ones in no contract — verify them, "
    "and return a deterministic verdict."
)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------

def _evidence_lines(conn, change_id: str) -> list[str]:
    lines: list[str] = []
    for item in core.evidence(conn, change_id):
        content = item.get("content") or {}
        detail = content.get("detail") or content.get("risk") or content.get("outcome")
        if detail:
            lines.append(f"{item['subject']}: {detail}")
    return lines


def answer(text: str, settings: Settings) -> str:
    """
    Produce the assistant's reply as markdown.

    The verdict comes from `core.gate_status()` and is written out verbatim.
    Narration, if enabled, is appended below it and clearly labelled.
    """
    intent = parse_intent(text, settings.components_root)
    if intent is None:
        return _HELP

    try:
        spec = core.build_spec(
            intent["kind"], intent["provider"], intent["old"],
            intent["new"], intent["components_root"],
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the endpoint
        return f"That change spec is not valid: {exc}"

    conn = core.open_ledger(settings.db_path)
    result = core.check(conn, f"{intent['old']} -> {intent['new']}", spec)
    gate = result["gate"]

    verdict = gate["result"]                      # verbatim, never generated
    icon = "✅" if verdict == "VERIFIED" else "❌"
    lines = [
        f"## {icon} {verdict}",
        "",
        gate["reason"],
        "",
    ]
    if gate.get("unresolved"):
        lines += [
            "**Blocking:** " + ", ".join(f"`{u}`" for u in gate["unresolved"]),
            "",
        ]
    if gate.get("work_items"):
        lines += ["| Component | Step | Status |", "| --- | --- | --- |"]
        for item in sorted(
            gate["work_items"], key=lambda w: (w["component"], w["step_kind"])
        ):
            mark = "✅" if item["status"] == "verified" else "❌"
            lines.append(
                f"| `{item['component']}` | {item['step_kind']} | {mark} {item['status']} |"
            )
        lines.append("")

    narration = watsonx.narrate(
        gate, _evidence_lines(conn, result["change_id"]), settings.watsonx
    )
    if narration:
        lines += ["**What this means**", "", narration, ""]

    lines.append(
        f"<sub>change `{result['change_id']}` · verdict from Interlock's "
        f"deterministic gate, which no model can override.</sub>"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

def _completion(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _stream(content: str, model: str) -> Iterator[str]:
    """
    Server-sent events in the OpenAI delta shape, terminated by [DONE].

    Chunked by line rather than token: the answer is already computed, so this
    is presentation. Streaming a verdict token-by-token would let a reader see a
    partial word like "NOT_PROVEN" render as something else mid-flight.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    yield frame({"role": "assistant", "content": ""})
    for line in content.splitlines(keepends=True):
        yield frame({"content": line})
    yield frame({}, finish="stop")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

def _authorise(settings: Settings, authorization: str | None) -> None:
    """
    Reject unless the caller presents the configured key.

    Disabled-by-default is deliberate: this endpoint runs test suites against a
    component tree, so serving it open would be remote code execution.
    """
    configured = settings.orchestrate.external_agent_key
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "The external-agent endpoint is disabled. Set "
                "INTERLOCK_EXTERNAL_AGENT_KEY to enable it."
            ),
        )
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if presented != configured:
        raise HTTPException(status_code=401, detail="invalid or missing credentials")


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    OpenAI-compatible completions, for registration as a watsonx Orchestrate
    external agent.

    Register the service URL with `/chat/completions` appended, and the key from
    INTERLOCK_EXTERNAL_AGENT_KEY as the API key.
    """
    settings: Settings = request.app.state.settings
    _authorise(settings, authorization)

    last_user = next(
        (m.content for m in reversed(body.messages) if m.role == "user" and m.content),
        "",
    )
    content = answer(last_user or "", settings)
    model = body.model or "interlock-change-safety"

    if body.stream:
        return StreamingResponse(
            _stream(content, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return _completion(content, model)
