"""
Dependency graph visualisation.

Nodes and edges come verbatim from GET /change-requests/{id}/graph.
Nothing is hardcoded: the provider is inferred as the node that only ever
appears on the ``from`` end of edges, and edge styling is driven by the
backend's ``edge_type`` field.

pyvis renders into an iframe, which the parent document's CSS custom properties
do not reach — so this is the one component that resolves the active palette to
concrete hex values rather than using ``var(--il-*)``.
"""

from __future__ import annotations

import html
import re

import streamlit as st
import streamlit.components.v1 as components

from utils import theme
from utils.theme import Palette, active


def _edge_style(p: Palette) -> dict[str, tuple[str, bool, str]]:
    """edge_type -> (colour, dashed, human label) for the active palette."""
    return {
        "api": (p.cyan, False, "API"),
        "event": (p.violet, False, "event"),
        "db": (p.green, False, "database"),
        "undocumented": (p.amber, True, "undocumented"),
    }


def _default_edge(p: Palette) -> tuple[str, bool, str]:
    return (p.muted, False, "dependency")


def _providers(edges: list[dict]) -> set[str]:
    sources = {e.get("from") for e in edges if e.get("from")}
    targets = {e.get("to") for e in edges if e.get("to")}
    return sources - targets


def _undocumented_targets(edges: list[dict]) -> set[str]:
    return {
        e["to"]
        for e in edges
        if e.get("to") and (
            e.get("documentation_status") == "undocumented"
            or e.get("edge_type") == "undocumented"
        )
    }


def _pyvis_html(nodes: list[dict], edges: list[dict], p: Palette) -> str | None:
    """Build an interactive pyvis network, or None if pyvis is unavailable."""
    try:
        from pyvis.network import Network
    except ImportError:
        return None

    net = Network(
        height="420px",
        width="100%",
        directed=True,
        bgcolor=p.term_bg,
        font_color=p.text,
        cdn_resources="in_line",
    )
    net.toggle_physics(True)
    net.set_options(
        """
        {
          "physics": {
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {"gravitationalConstant": -70,
                                 "springLength": 150,
                                 "avoidOverlap": 0.6},
            "stabilization": {"iterations": 180}
          },
          "interaction": {"hover": true, "tooltipDelay": 120},
          "edges": {"smooth": {"type": "dynamic"},
                    "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}}}
        }
        """
    )

    providers = _providers(edges)
    undocumented = _undocumented_targets(edges)
    styles = _edge_style(p)

    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            continue
        if node_id in providers:
            colour, size, role = p.cyan, 34, "provider"
        elif node_id in undocumented:
            colour, size, role = p.amber, 26, "undocumented consumer"
        else:
            colour, size, role = p.green, 24, "consumer"
        net.add_node(
            node_id,
            label=node.get("label", node_id),
            color={"background": colour, "border": colour,
                   "highlight": {"background": colour, "border": p.text}},
            size=size,
            shape="dot",
            font={"color": p.text, "size": 15},
            title=f"{node_id} — {role}",
        )

    for edge in edges:
        src, dst = edge.get("from"), edge.get("to")
        if not src or not dst:
            continue
        colour, dashed, label = styles.get(
            edge.get("edge_type", ""), _default_edge(p)
        )
        dashed = dashed or edge.get("documentation_status") == "undocumented"
        reason = edge.get("reason") or ""
        net.add_edge(
            src,
            dst,
            color=colour,
            dashes=dashed,
            width=3 if dashed else 2,
            label=label,
            font={"color": p.muted, "size": 10, "strokeWidth": 0},
            title=reason or label,
        )

    try:
        doc = net.generate_html(notebook=False)
    except Exception:  # pragma: no cover - template/render issues
        return None

    return _restyle(doc, p)


# pyvis templates pull Bootstrap and vis.css from a CDN.  Those are only needed
# by the filter/select widgets we do not use, and they cost the demo a network
# round-trip — and Bootstrap's own `body` rule repaints the frame white.
_EXTERNAL_STYLESHEET = re.compile(
    r"<link[^>]*rel=[\"']stylesheet[\"'][^>]*>", re.IGNORECASE | re.DOTALL
)

# The Bootstrap bundle is likewise only used by the unused widgets.  vis.js
# itself is inlined via cdn_resources="in_line" and is left untouched.
_EXTERNAL_SCRIPT = re.compile(
    r"<script[^>]*src=[\"']https?://[^\"']*[\"'][^>]*>\s*</script>",
    re.IGNORECASE | re.DOTALL,
)


def _restyle(doc: str, p: Palette) -> str:
    """Drop CDN assets and force the document to the panel background."""
    overrides = (
        "<style>"
        f"html,body{{margin:0;padding:0;background:{p.term_bg} !important;}}"
        f"#mynetwork{{border:none !important;background:{p.term_bg} !important;}}"
        "</style>"
    )
    doc = _EXTERNAL_STYLESHEET.sub("", doc)
    doc = _EXTERNAL_SCRIPT.sub("", doc)
    if "</head>" in doc:
        # Injected last so it wins over the template's own rules.
        return doc.replace("</head>", overrides + "</head>", 1)
    return overrides + doc


def _fallback_html(edges: list[dict], p: Palette) -> str:
    """Dependency-free rendering used when pyvis cannot render."""
    styles = _edge_style(p)
    rows = []
    for edge in edges:
        colour, dashed, label = styles.get(
            edge.get("edge_type", ""), _default_edge(p)
        )
        dashed = dashed or edge.get("documentation_status") == "undocumented"
        rows.append(
            f'<div class="il-consumer">'
            f'<span class="name">{html.escape(edge.get("from", "?"))} '
            f'<span style="color:var(--il-muted)">──▶</span> '
            f'{html.escape(edge.get("to", "?"))}</span>'
            f'<span style="color:{colour}">{html.escape(label)}</span>'
            f"</div>"
            f'<div style="color:var(--il-muted);font-size:0.72rem;'
            f'margin:-0.15rem 0 0.5rem 0.6rem">'
            f'{html.escape(edge.get("reason", ""))}</div>'
        )
    return "".join(rows)


def _embed(html_doc: str, *, height: int) -> None:
    """
    Embed a self-contained HTML document.

    ``st.components.v1.html`` is deprecated in favour of ``st.iframe``; use the
    new API where the installed Streamlit provides it and fall back otherwise.

    The two signatures differ: ``st.iframe(src, *, width, height, tab_index)``
    has no ``scrolling`` argument, so it must not be forwarded there.
    """
    embed = getattr(st, "iframe", None)
    if embed is not None:
        embed(html_doc, height=height)
    else:  # pragma: no cover - older Streamlit
        components.html(html_doc, height=height, scrolling=False)


def render_graph(graph: dict | None, error: str | None = None) -> None:
    """Render the dependency graph panel."""
    p = active()

    st.markdown(
        theme.panel_header(
            "Dependency graph",
            "Built from the dependency edges the agents discovered. Cyan is the "
            "provider being changed, green a documented consumer, amber one "
            "found only in source. Dashed edges are undocumented. No component "
            "is hardcoded here.",
        ),
        unsafe_allow_html=True,
    )

    if error:
        st.markdown("</div>", unsafe_allow_html=True)
        st.error(f"Graph unavailable — {error}")
        return

    edges = (graph or {}).get("edges", [])
    nodes = (graph or {}).get("nodes", [])

    if not nodes:
        st.markdown(
            '<div class="il-empty">No dependencies discovered yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    styles = _edge_style(p)
    legend = []
    seen_types = {e.get("edge_type") for e in edges}
    for edge_type in sorted(t for t in seen_types if t):
        colour, dashed, label = styles.get(edge_type, _default_edge(p))
        border = "dashed" if dashed else "solid"
        legend.append(
            f'<span class="il-chip" style="color:{colour};'
            f"border:1px {border} {colour};"
            f'background:transparent">{html.escape(label)}</span>'
        )
    st.markdown(
        f'<div style="margin-bottom:0.5rem">{"".join(legend)}</div>',
        unsafe_allow_html=True,
    )

    rendered = _pyvis_html(nodes, edges, p)
    if rendered:
        _embed(rendered, height=430)
    else:
        st.markdown(_fallback_html(edges, p), unsafe_allow_html=True)
        st.caption("Interactive view unavailable — showing edge list.")

    st.markdown("</div>", unsafe_allow_html=True)


def render_hidden_dependency_callout(hidden: list[dict]) -> None:
    """Highlight undocumented edges — the core reveal of the demo."""
    if not hidden:
        return
    p = active()
    items = "".join(
        f'<div style="font-family:ui-monospace,Menlo,Consolas,monospace;'
        f'font-size:0.85rem;color:var(--il-text);padding:0.2rem 0">'
        f'<span style="color:{p.amber}">▲</span> '
        f'{html.escape(e.get("to", "?"))} '
        f'<span style="color:var(--il-muted)">— {html.escape(e.get("reason", ""))}</span>'
        f"</div>"
        for e in hidden
    )
    st.markdown(
        f'<div class="il-panel" style="border-color:{p.amber};'
        f'background:transparent">'
        f'<div class="il-panel-head">'
        f'<div class="il-panel-title" style="color:{p.amber}">'
        f"Undocumented dependencies discovered</div>"
        + theme.info_icon(
            "Undocumented dependencies discovered",
            "Consumers reached by a dependency the published contract never "
            "declared, found by reading source. These are the ones a rename "
            "would break silently, and the gate still requires them to be "
            "verified.",
        )
        + f"</div>{items}"
        f'<div style="color:var(--il-muted);font-size:0.75rem;margin-top:0.5rem">'
        f"Found in source, absent from the published contract."
        f"</div></div>",
        unsafe_allow_html=True,
    )
