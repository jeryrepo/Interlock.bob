"""
frontend/utils/theme.py
========================
Single place for the Interlock visual language.

Two palettes — a dark "control room" and a light "daylight" variant — follow
whichever theme Streamlit is rendering (Settings → Appearance, or the system
preference). The semantic roles are identical in both:

    cyan    the system / the provider under change
    amber   waiting on a human, or an undocumented dependency
    red     blocked
    green   proven safe

Components style themselves with the ``--il-*`` CSS custom properties emitted by
``inject()`` rather than literal hex, so a palette change needs no edits to them.
Only the dependency graph reads concrete values, because pyvis renders into an
iframe that CSS variables from this document do not reach.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class Palette:
    """Concrete colours for one theme."""

    name: str
    bg: str
    panel: str
    panel_edge: str
    text: str
    muted: str
    faint: str          # dimmest readable tone (terminal timestamps)
    term_bg: str
    cyan: str
    green: str
    amber: str
    red: str
    violet: str
    passport_from: str  # passport gradient start
    passport_to: str    # passport gradient end


DARK = Palette(
    name="dark",
    bg="#0b0f14",
    panel="#121821",
    panel_edge="#1e2733",
    text="#e6edf3",
    muted="#8b98a8",
    faint="#4b5b6b",
    term_bg="#070a0e",
    cyan="#22d3ee",
    green="#34d399",
    amber="#fbbf24",
    red="#f87171",
    violet="#a78bfa",
    passport_from="#101822",
    passport_to="#0b0f14",
)

# Accents are darkened for the light palette so they keep AA contrast against a
# near-white panel; the hues stay the same so the two themes read as one design.
LIGHT = Palette(
    name="light",
    bg="#f4f6f9",
    panel="#ffffff",
    panel_edge="#d9e0e8",
    text="#0f172a",
    muted="#5a6b7f",
    faint="#8a99a9",
    term_bg="#eef2f7",
    cyan="#0e7490",
    green="#047857",
    amber="#b45309",
    red="#b91c1c",
    violet="#6d28d9",
    passport_from="#ffffff",
    passport_to="#eef4f8",
)


def _vars(p: Palette) -> str:
    """Emit one palette as ``--il-*`` custom properties."""
    # Tint/border alphas live in the palette too, so a single :root swap
    # re-themes every derived colour without re-running any Python.
    tint = "12%" if p.name == "dark" else "10%"
    edge_a = "35%" if p.name == "dark" else "45%"
    return f"""
      --il-bg: {p.bg};
      --il-panel: {p.panel};
      --il-edge: {p.panel_edge};
      --il-text: {p.text};
      --il-muted: {p.muted};
      --il-faint: {p.faint};
      --il-term-bg: {p.term_bg};
      --il-cyan: {p.cyan};
      --il-green: {p.green};
      --il-amber: {p.amber};
      --il-red: {p.red};
      --il-violet: {p.violet};
      --il-pp-from: {p.passport_from};
      --il-pp-to: {p.passport_to};
      --il-tint: {tint};
      --il-edge-a: {edge_a};
    """


def tint(var_name: str, amount: str = "var(--il-tint)") -> str:
    """A translucent wash of a palette colour, usable as a background."""
    return f"color-mix(in srgb, var({var_name}) {amount}, transparent)"


def active() -> Palette:
    """
    Return the palette matching Streamlit's current theme.

    ``st.context.theme.type`` is documented as possibly lagging on the very
    first run of a session or immediately after the user flips the theme, so
    anything unexpected falls back to dark rather than raising.
    """
    try:
        return LIGHT if st.context.theme.type == "light" else DARK
    except Exception:  # pragma: no cover - older/embedded runtimes
        return DARK


def _css(resolved: Palette) -> str:
    return f"""
<style>
  /* 1. Dark is the base. */
  :root {{ {_vars(DARK)} }}

  /* 2. Follow the browser/system preference immediately, so a page that opens
        in light mode is correct on first paint without waiting for Python. */
  @media (prefers-color-scheme: light) {{
      :root {{ {_vars(LIGHT)} }}
  }}

  /* 3. Streamlit's resolved theme is authoritative and therefore last.  It is
        one rerun behind a manual Settings -> Appearance switch, which the
        media query above already covers for the common case. */
  :root {{ {_vars(resolved)} }}

  .stApp {{ background: var(--il-bg); }}
  section.main > div {{ padding-top: 1.2rem; }}

  /* The sidebar keeps its own surface, so paint it explicitly rather than
     relying on the app background bleeding through. */
  [data-testid="stSidebar"] {{
      background: var(--il-panel); border-right: 1px solid var(--il-edge);
  }}
  [data-testid="stSidebar"] h3 {{
      color: var(--il-muted); font-size: 0.72rem; font-weight: 600;
      letter-spacing: 0.14em; text-transform: uppercase;
  }}
  [data-testid="stHeader"] {{ background: transparent; }}

  /* Sidebar inputs carry identifiers and field names — render them as code. */
  [data-testid="stSidebar"] input {{
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 0.82rem;
  }}
  [data-testid="stSidebar"] [data-testid="stExpander"] {{
      border: 1px solid var(--il-edge); border-radius: 10px;
      background: transparent;
  }}
  [data-testid="stSidebar"] [data-testid="stExpander"] summary {{
      font-size: 0.78rem; color: var(--il-muted);
  }}

  .il-title {{
      font-size: 2.1rem; font-weight: 700; letter-spacing: -0.02em;
      color: var(--il-text); margin: 0;
  }}
  .il-title span {{ color: var(--il-cyan); }}
  .il-sub {{ color: var(--il-muted); font-size: 0.9rem; margin-top: 0.15rem; }}

  .il-panel {{
      background: var(--il-panel); border: 1px solid var(--il-edge);
      border-radius: 12px; padding: 1rem 1.15rem; margin-bottom: 0.8rem;
  }}
  .il-panel-title {{
      color: var(--il-muted); font-size: 0.72rem; font-weight: 600;
      letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 0.6rem;
  }}

  .il-chip {{
      display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
      font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em;
      border: 1px solid transparent; margin-right: 0.3rem;
  }}
  .il-chip-ok      {{ color: var(--il-green); background: color-mix(in srgb, var(--il-green) var(--il-tint), transparent); border-color: color-mix(in srgb, var(--il-green) var(--il-edge-a), transparent); }}
  .il-chip-wait    {{ color: var(--il-amber); background: color-mix(in srgb, var(--il-amber) var(--il-tint), transparent); border-color: color-mix(in srgb, var(--il-amber) var(--il-edge-a), transparent); }}
  .il-chip-blocked {{ color: var(--il-red);   background: color-mix(in srgb, var(--il-red) var(--il-tint), transparent);   border-color: color-mix(in srgb, var(--il-red) var(--il-edge-a), transparent); }}
  .il-chip-info    {{ color: var(--il-cyan);  background: color-mix(in srgb, var(--il-cyan) var(--il-tint), transparent);  border-color: color-mix(in srgb, var(--il-cyan) var(--il-edge-a), transparent); }}
  .il-chip-muted   {{ color: var(--il-muted); background: color-mix(in srgb, var(--il-muted) 10%, transparent); border-color: color-mix(in srgb, var(--il-muted) 30%, transparent); }}

  .il-change {{
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 1.6rem; font-weight: 600; color: var(--il-text);
      line-height: 1.35;
  }}
  .il-change .from {{ color: var(--il-red); text-decoration: line-through; }}
  .il-change .arrow {{ color: var(--il-muted); margin: 0 0.5rem; }}
  .il-change .to {{ color: var(--il-green); }}

  /* --- state rail --- */
  .il-rail {{ display: flex; gap: 4px; margin: 0.2rem 0 0.5rem; }}
  .il-rail-step {{
      flex: 1; height: 6px; border-radius: 3px; background: var(--il-edge);
  }}
  .il-rail-step.done {{ background: var(--il-cyan); }}
  .il-rail-step.current {{ background: var(--il-amber); }}
  .il-rail-labels {{
      display: flex; gap: 4px; font-size: 0.6rem; color: var(--il-muted);
      text-transform: uppercase; letter-spacing: 0.05em;
  }}
  .il-rail-labels div {{ flex: 1; text-align: center; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; }}
  .il-rail-labels div.current {{ color: var(--il-amber); font-weight: 700; }}

  /* --- terminal feed --- */
  .il-term {{
      background: var(--il-term-bg); border: 1px solid var(--il-edge);
      border-radius: 10px;
      padding: 0.8rem 0.9rem; height: 380px; overflow-y: auto;
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 0.78rem; line-height: 1.55;
  }}
  .il-term .row {{ white-space: pre-wrap; word-break: break-word; }}
  .il-term .t  {{ color: var(--il-faint); }}
  .il-term .ph {{ color: var(--il-violet); }}
  .il-term .sj {{ color: var(--il-text); }}
  .il-term .ok    {{ color: var(--il-green); }}
  .il-term .alert {{ color: var(--il-amber); font-weight: 700; }}
  .il-term .error {{ color: var(--il-red); }}
  .il-term .d  {{ color: var(--il-muted); }}
  .il-term .cursor {{ color: var(--il-cyan); }}

  /* --- gate --- */
  .il-gate {{ border-radius: 12px; padding: 1.1rem 1.2rem; text-align: center; }}
  .il-gate-verdict {{
      font-family: ui-monospace, Menlo, Consolas, monospace;
      font-size: 1.7rem; font-weight: 800; letter-spacing: 0.06em;
  }}
  .il-gate-reason {{ color: var(--il-muted); font-size: 0.82rem; margin-top: 0.4rem; }}
  .il-gate-ok      {{ background: color-mix(in srgb, var(--il-green) 8%, transparent); border: 1px solid color-mix(in srgb, var(--il-green) 40%, transparent); }}
  .il-gate-ok .il-gate-verdict {{ color: var(--il-green); }}
  .il-gate-blocked {{ background: color-mix(in srgb, var(--il-red) 8%, transparent); border: 1px solid color-mix(in srgb, var(--il-red) 40%, transparent); }}
  .il-gate-blocked .il-gate-verdict {{ color: var(--il-red); }}
  .il-gate-pending {{ background: color-mix(in srgb, var(--il-muted) 6%, transparent); border: 1px solid var(--il-edge); }}
  .il-gate-pending .il-gate-verdict {{ color: var(--il-muted); }}

  /* --- consumer rows --- */
  .il-consumer {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.4rem 0.6rem; border-radius: 8px; margin-bottom: 0.3rem;
      background: color-mix(in srgb, var(--il-muted) 6%, transparent); border: 1px solid var(--il-edge);
      font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.8rem;
  }}
  .il-consumer .name {{ color: var(--il-text); }}
  .il-consumer .st-verified    {{ color: var(--il-green); }}
  .il-consumer .st-pending     {{ color: var(--il-muted); }}
  .il-consumer .st-in_progress {{ color: var(--il-amber); }}
  .il-consumer .st-failed      {{ color: var(--il-red); }}

  /* --- passport --- */
  .il-passport {{
      background: linear-gradient(160deg, var(--il-pp-from) 0%, var(--il-pp-to) 100%);
      border: 1px solid color-mix(in srgb, var(--il-cyan) var(--il-edge-a), transparent); border-radius: 14px;
      padding: 1.3rem 1.5rem;
  }}
  .il-passport h3 {{ color: var(--il-cyan); margin: 0 0 0.1rem; font-size: 1.15rem;
      letter-spacing: 0.02em; }}
  .il-pp-row {{ display: flex; padding: 0.35rem 0;
      border-bottom: 1px dashed var(--il-edge); font-size: 0.85rem; }}
  .il-pp-row:last-child {{ border-bottom: none; }}
  .il-pp-key {{ width: 210px; flex: none; color: var(--il-muted);
      text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.1em;
      padding-top: 0.15rem; }}
  .il-pp-val {{ color: var(--il-text); font-family: ui-monospace, Menlo, Consolas, monospace; }}

  /* --- panel header with an (i) explainer --- */
  .il-panel-head {{
      display: flex; align-items: flex-start; justify-content: space-between;
      gap: 0.5rem;
  }}
  .il-info {{
      position: relative; flex: none; cursor: help;
      width: 16px; height: 16px; border-radius: 50%;
      border: 1px solid var(--il-edge); background: transparent;
      color: var(--il-muted); font-size: 0.66rem; font-weight: 700;
      font-style: normal; font-family: ui-sans-serif, system-ui, sans-serif;
      display: inline-flex; align-items: center; justify-content: center;
      line-height: 1; user-select: none;
  }}
  .il-info:hover, .il-info:focus {{
      color: var(--il-cyan); border-color: var(--il-cyan); outline: none;
  }}
  .il-tip {{
      position: absolute; top: 22px; right: 0; width: 260px;
      background: var(--il-panel); color: var(--il-text);
      border: 1px solid var(--il-edge); border-radius: 8px;
      padding: 0.6rem 0.7rem;
      font-size: 0.72rem; font-weight: 400; line-height: 1.5;
      letter-spacing: 0; text-transform: none; text-align: left;
      box-shadow: 0 10px 28px color-mix(in srgb, black 35%, transparent);
      opacity: 0; visibility: hidden; transition: opacity 0.12s ease;
      z-index: 999; pointer-events: none;
  }}
  .il-info:hover .il-tip, .il-info:focus .il-tip {{
      opacity: 1; visibility: visible;
  }}

  .il-empty {{ color: var(--il-muted); font-size: 0.82rem; font-style: italic; }}
</style>
"""


def inject() -> Palette:
    """
    Inject the stylesheet for the active theme and return its palette.

    Called once per rerun, before anything renders.
    """
    palette = active()
    st.markdown(_css(palette), unsafe_allow_html=True)
    return palette


def chip(text: str, kind: str = "muted") -> str:
    return f'<span class="il-chip il-chip-{kind}">{text}</span>'


def info_icon(label: str, info: str) -> str:
    """A standalone (i) explainer, for panels that build their own markup."""
    safe_label, safe_info = html.escape(label), html.escape(info)
    return (
        f'<span class="il-info" tabindex="0" role="note" '
        f'aria-label="{safe_label}: {safe_info}">i'
        f'<span class="il-tip">{safe_info}</span></span>'
    )


def panel_header(title: str, info: str | None = None) -> str:
    """
    Opening markup for a panel: ``<div class="il-panel">`` plus its title.

    ``info`` adds a hoverable/focusable (i) that explains what the panel shows
    and where its numbers come from.  The caller still closes the ``</div>``.
    """
    safe_title = html.escape(title)
    if not info:
        return f'<div class="il-panel"><div class="il-panel-title">{safe_title}</div>'
    return (
        '<div class="il-panel"><div class="il-panel-head">'
        f'<div class="il-panel-title">{safe_title}</div>'
        f'{info_icon(title, info)}'
        '</div>'
    )
