"""
agents/discovery/polyglot_source.py
====================================
polyglot-source-discovery agent: finds consumers written in languages the
Python-AST agents cannot see.

Why this exists
---------------
Every other discovery agent parses Python. A TypeScript checkout service or a
Java fraud worker consuming the same contract was therefore invisible: no
dependency edge, so the gate never required it, so a migration could be
declared VERIFIED while an entire consumer was never migrated. For a safety
tool, discovery completeness IS the safety property — an unfound consumer is
a false VERIFIED waiting to happen. This agent's job is to make such
components visible so the gate can refuse to pass them until they are proven.

How it finds references
-----------------------
Lexical scanning with language-aware patterns, not full parsers. Three forms:

1. **Quoted wire name** — ``"customer_id"`` — the JSON key as it crosses the
   wire. Identical in every language (Jackson's ``@JsonProperty``, Go struct
   tags, ``obj["customer_id"]``, ``getString("customer_id")``), so it is the
   highest-signal pattern and is marked ``confirmed``.
2. **Bare identifier** — ``customer_id`` as a standalone symbol — for
   languages that keep wire names as identifiers (JS/TS destructuring and
   property access, Ruby symbols, PHP properties). Also ``confirmed``.
3. **Naming-convention variants** — ``customerId`` / ``CustomerId`` — for
   languages whose style renames the field at the mapping layer (Java, Kotlin,
   C#, Go). A convention match is an inference, not an observation, so these
   are marked ``hypothesis``. The dependency edge is still emitted: the gate
   must know about a *probable* consumer, and a human can refute it.

What it deliberately does not do
--------------------------------
- Never scans ``.py`` (the AST agents own Python) or ``.sql`` (db-schema owns
  schemas) — no duplicate claims about files another agent reads properly.
- Never walks dependency/build directories (``node_modules``, ``target``,
  ``vendor``…). On a real JS repo that is the difference between scanning a
  service and scanning the npm registry.
- Never writes the ledger, never calls another agent, never decides the gate.

Returns a dict that validates as DiscoveryResult.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchestrator.schemas import Dependency, DiscoveryResult, Evidence

# One definition of what counts as a component, shared with repo_map.
from agents.discovery.repo_map import component_dirs as component_dirs_shared

# ---------------------------------------------------------------------------
# Language map
# ---------------------------------------------------------------------------

# Which reference forms apply per language:
#   quoted  — "field" / 'field'  (wire name in a string literal)
#   ident   — field as a standalone identifier
#   camel   — customerId          (convention variant, hypothesis)
#   pascal  — CustomerId          (convention variant, hypothesis; also
#             matches inside getCustomerId/setCustomerId)
_LANGUAGES: dict[str, tuple[str, ...]] = {
    "javascript": ("quoted", "ident", "camel"),
    "typescript": ("quoted", "ident", "camel"),
    "java": ("quoted", "camel", "pascal"),
    "kotlin": ("quoted", "camel", "pascal"),
    "scala": ("quoted", "camel", "pascal"),
    "csharp": ("quoted", "camel", "pascal"),
    "go": ("quoted", "pascal"),
    "ruby": ("quoted", "ident"),
    "php": ("quoted", "ident"),
}

_EXT_TO_LANG: dict[str, str] = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala",
    ".cs": "csharp",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
}

# Directories that hold other people's code or build output. Walking them
# turns "scan this service" into "scan the npm registry" and manufactures
# false consumers out of vendored libraries.
_SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn",
    ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", "out", "target", "vendor", "coverage",
    ".next", ".nuxt", ".gradle", ".idea", ".vs", "bin", "obj",
}

# Generated bundles are one line of minified soup; a hit inside one says
# nothing actionable and the file can be megabytes.
_SKIP_SUFFIX_PATTERNS = (".min.js", ".min.css", ".bundle.js", ".map")

_MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Symbol variants
# ---------------------------------------------------------------------------

def _camel(symbol: str) -> str:
    """customer_id -> customerId; deliver_via_webhook -> deliverViaWebhook."""
    head, *rest = symbol.split("_")
    return head + "".join(part.capitalize() for part in rest if part)


def _pascal(symbol: str) -> str:
    """customer_id -> CustomerId."""
    return "".join(part.capitalize() for part in symbol.split("_") if part)


def _patterns_for(symbol: str, forms: tuple[str, ...]) -> list[tuple[re.Pattern, str, str]]:
    """
    Compile the reference patterns for one symbol in one language.

    Returns (pattern, matched_variant, confidence) triples. Convention
    variants exist only when the symbol actually has underscores to convert —
    for a single-word symbol they would duplicate the identifier pattern.
    """
    out: list[tuple[re.Pattern, str, str]] = []
    esc = re.escape(symbol)

    if "quoted" in forms:
        out.append((re.compile(r'["\']' + esc + r'["\']'), symbol, "confirmed"))
    if "ident" in forms:
        out.append((
            re.compile(r"(?<![A-Za-z0-9_])" + esc + r"(?![A-Za-z0-9_])"),
            symbol, "confirmed",
        ))

    if "_" in symbol:
        camel = _camel(symbol)
        pascal = _pascal(symbol)
        if "camel" in forms and camel != symbol:
            out.append((
                re.compile(r"(?<![A-Za-z0-9_])" + re.escape(camel) + r"(?![A-Za-z0-9_])"),
                camel, "hypothesis",
            ))
        if "pascal" in forms and pascal != symbol:
            # No left guard, deliberately: the Pascal form appears mid-identifier
            # in getters and builders (getCustomerId, withCustomerId), and those
            # are exactly the references worth finding.
            out.append((
                re.compile(re.escape(pascal) + r"(?![A-Za-z0-9_])"),
                pascal, "hypothesis",
            ))
    return out


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _iter_source_files(component_dir: Path):
    for path in sorted(component_dir.rglob("*")):
        if not path.is_file():
            continue
        if _SKIP_DIRS.intersection(path.relative_to(component_dir).parts[:-1]):
            continue
        if path.suffix not in _EXT_TO_LANG:
            continue
        if any(path.name.endswith(s) for s in _SKIP_SUFFIX_PATTERNS):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _scan_component(
    component_dir: Path, root: Path, symbols: list[str]
) -> list[dict[str, Any]]:
    """All references to any symbol in one component, with file:line detail."""
    refs: list[dict[str, Any]] = []
    for path in _iter_source_files(component_dir):
        lang = _EXT_TO_LANG[path.suffix]
        patterns = [
            trip
            for symbol in symbols
            for trip in _patterns_for(symbol, _LANGUAGES[lang])
        ]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, variant, confidence in patterns:
                if pattern.search(line):
                    refs.append({
                        "file": rel,
                        "line": lineno,
                        "lang": lang,
                        "matched": variant,
                        "confidence": confidence,
                    })
                    break  # one ref per line is enough
    return refs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(data: dict[str, Any]) -> dict[str, Any]:
    """
    Expected keys in ``data``:
      change_id        (str)
      components_root  (str)  — directory whose subdirectories are components
                                (``fixtures_root`` accepted as a fallback)
      provider         (str)
      old_field        (str)
      new_field        (str, optional) — also matched, so a consumer that has
                        already migrated is still discovered as a consumer.
    """
    change_id: str = data["change_id"]
    provider: str = data.get("provider", "account-service")
    old_field: str = data.get("old_field", "customer_id")
    new_field: str = data.get("new_field") or ""

    root = Path(data.get("components_root") or data.get("fixtures_root") or ".").resolve()

    symbols = [old_field]
    if new_field and new_field != old_field:
        symbols.append(new_field)

    evidence: list[Evidence] = []
    dependencies: list[Dependency] = []

    for component_dir in component_dirs_shared(root):
        name = component_dir.name
        refs = _scan_component(component_dir, root, symbols)
        if not refs:
            continue

        langs = sorted({r["lang"] for r in refs})
        # The component-level claim is only as strong as its best reference: a
        # component seen solely through naming-convention variants is a
        # hypothesis to verify, not an observed fact.
        confidence = (
            "confirmed"
            if any(r["confidence"] == "confirmed" for r in refs)
            else "hypothesis"
        )
        first = refs[0]

        evidence.append(Evidence(
            claim_type="dependency",
            subject=name,
            content={
                "component": name,
                "provider": provider,
                "field": old_field,
                "languages": langs,
                "refs": refs,
                "detection_method": "polyglot lexical scan",
            },
            source_ref=f"{first['file']}:{first['line']}",
            confidence=confidence,  # type: ignore[arg-type]
        ))

        if name != provider:
            dependencies.append(Dependency(
                from_component=provider,
                to_component=name,
                edge_type="undocumented",
                reason=(
                    f"{'/'.join(langs)} source references "
                    f"{sorted({r['matched'] for r in refs})} "
                    f"({len(refs)} ref(s), {confidence})"
                ),
            ))

    return DiscoveryResult(
        change_id=change_id,
        evidence=evidence,
        dependencies=dependencies,
    ).model_dump()
