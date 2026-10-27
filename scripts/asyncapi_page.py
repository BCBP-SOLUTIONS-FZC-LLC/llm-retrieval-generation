"""Render ``api/asyncapi.html``: the BCBP-themed event catalogue (port of the IAM services' page).

Same layout, theme, search, deep links and light/dark toggle as iam-org-membership's
``/asyncapi`` (CSS and JS are copied verbatim into ``asyncapi_page.css`` / ``.js``). Rendered
once at generation time from the AsyncAPI document, so the service needs neither a YAML parser
nor a renderer at runtime; ``{{ENV}}`` is the one placeholder, filled when the page is served.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMA_PREFIX = "#/components/schemas/"
ENV_PLACEHOLDER = "{{ENV}}"


def esc(value: object) -> str:
    """HTML-escape ``value``."""
    return html.escape(str(value))


def _short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _is_consumed(message: dict[str, Any]) -> bool:
    return any(str(t.get("$ref", "")).endswith("/consumed") for t in message.get("tags", []))


def _sns_event_type(message: dict[str, Any]) -> str:
    attrs = message.get("bindings", {}).get("sns", {}).get("messageAttributes", {})
    return str(attrs.get("EventType", {}).get("value", ""))


def _variants(prop: dict[str, Any]) -> list[dict[str, Any]]:
    """``anyOf`` members (pydantic's Optional[X] is ``anyOf: [X, null]``), else the property."""
    return list(prop["anyOf"]) if "anyOf" in prop else [prop]


def _type_text(prop: dict[str, Any]) -> str:
    parts = []
    for v in _variants(prop):
        if "$ref" in v:
            parts.append(v["$ref"].removeprefix(SCHEMA_PREFIX))
        elif v.get("type") == "array":
            items = v.get("items", {})
            inner = items.get("$ref", "").removeprefix(SCHEMA_PREFIX) or items.get("type", "any")
            parts.append(f"array<{inner}>")
        else:
            t = v.get("type", "any")
            parts.append(f"{t}({v['format']})" if v.get("format") else str(t))
    return " | ".join(parts)


def _type_html(prop: dict[str, Any]) -> str:
    refs = [v["$ref"].removeprefix(SCHEMA_PREFIX) for v in _variants(prop) if "$ref" in v]
    if len(refs) == 1 and len(_variants(prop)) == 1:
        n = esc(refs[0])
        return f'<a href="#schema-{n}" class="prop-type schema-link">{n}</a>'
    return f'<span class="prop-type">{esc(_type_text(prop))}</span>'


def _enums(prop: dict[str, Any]) -> list[Any]:
    for v in _variants(prop):
        if "enum" in v:
            return list(v["enum"])
        if "enum" in v.get("items", {}):
            return list(v["items"]["enum"])
    return []


def _props_table(schema: dict[str, Any], schema_id: str) -> str:
    props: dict[str, Any] = schema.get("properties", {})
    if not props:
        return '<p style="color:var(--muted);font-size:.8rem">No properties.</p>'
    required = set(schema.get("required", []))
    keys = sorted(props, key=lambda k: k not in required)  # stable: required first
    rows = []
    for k in keys:
        p = props[k]
        req = '<span class="prop-req">required</span>' if k in required else ""
        desc = _short(str(p.get("description", "")), 200)
        enum = _enums(p)
        enum_html = (
            '<div class="enum-list">'
            + "".join(f'<span class="enum-val">{esc(e)}</span>' for e in enum)
            + "</div>"
            if enum
            else ""
        )
        example = ""
        if "example" in p:
            example = (
                '<pre style="background:#0d0d0d;border:1px solid rgba(132,38,176,.25);'
                "padding:6px 8px;border-radius:6px;font-size:.72rem;margin-top:6px;color:#e879f9;"
                f'overflow-x:auto;white-space:pre-wrap">{esc(json.dumps(p["example"], indent=2))}</pre>'
            )
        rows.append(
            f'<tr id="field--{esc(schema_id)}--{esc(k)}">'
            f'<td onclick="copyText(\'{esc(k)}\')" title="Click to copy" style="cursor:pointer">'
            f'<span class="prop-name">{esc(k)}</span></td>'
            f"<td>{_type_html(p)}</td><td>{req}</td>"
            f'<td><span class="prop-desc">{esc(desc)}</span>{enum_html}{example}</td></tr>'
        )
    return (
        '<table class="prop-table"><thead><tr><th>Field</th><th>Type</th><th>Required</th>'
        "<th>Description</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _card(card_id: str, header: str, body: str) -> str:
    return (
        f'<div class="card" id="{esc(card_id)}"><div class="card-header">{header}'
        '<button class="toggle-btn" type="button" tabindex="-1" aria-hidden="true">▸</button>'
        f'</div><div class="card-body" style="display:none">{body}</div></div>'
    )


def _message(name: str, msg: dict[str, Any], schemas: dict[str, Any]) -> str:
    consumed = _is_consumed(msg)
    badge = (
        '<span class="badge m-recv">RECEIVE</span>'
        if consumed
        else '<span class="badge m-send">SEND</span>'
    )
    event_type = _sns_event_type(msg)
    attr = f'<span class="sns-attr">event_type: {esc(event_type)}</span>' if event_type else ""
    header = f'{badge}<span class="card-title">{esc(msg.get("title") or name)}</span>{attr}'
    body = ""
    if summary := str(msg.get("summary", "")).strip():
        body += f'<p class="card-summary" style="margin-bottom:.75rem">{esc(summary)}</p>'
    ref = str(msg.get("payload", {}).get("$ref", "")).removeprefix(SCHEMA_PREFIX)
    if ref in schemas:
        body += (
            '<div style="font-size:.78rem;color:var(--muted);margin-bottom:.5rem">Payload: '
            f'<span style="color:var(--code);font-family:monospace">{esc(ref)}</span></div>'
            + _props_table(schemas[ref], ref)
        )
    return _card(f"msg-{name}", header, body)


def _servers(servers: dict[str, Any]) -> str:
    items = "".join(
        f'<div class="server-item"><div><span class="server-name">{esc(n)}</span></div>'
        f'<div><span class="server-proto">{esc(str(s.get("protocol", "")).upper())}</span> '
        f'<span class="server-host">{esc(s.get("host", ""))}</span></div>'
        f'<div class="server-desc">{esc(_short(str(s.get("description", "")), 200))}</div></div>'
        for n, s in servers.items()
    )
    return (
        '<div class="section" id="servers"><div class="section-title">Servers</div>'
        f'<div class="card">{items}</div></div>'
    )


def render(doc: dict[str, Any]) -> str:
    """The catalogue page for an AsyncAPI 3.0 ``doc``."""
    info = doc["info"]
    schemas: dict[str, Any] = doc["components"]["schemas"]
    messages: dict[str, Any] = doc["components"]["messages"]
    published = sorted(n for n, m in messages.items() if not _is_consumed(m))
    consumed = sorted(n for n, m in messages.items() if _is_consumed(m))

    def links(prefix: str, names: list[str]) -> str:
        return "".join(f'<a href="#{prefix}-{esc(n)}">{esc(n)}</a>' for n in names)

    topics = [c["address"] for c in doc["channels"].values() if _topic(c, doc)]
    sidebar = (
        '<div style="padding:.75rem 1rem .25rem"><input id="search" placeholder="Search..." '
        'autocomplete="off" style="width:100%;padding:6px 10px;background:#0d0d0d;border:1px solid '
        'var(--border);color:#fff;border-radius:6px;font-size:.8rem;outline:none"></div>'
        '<div class="sidebar-group"><div class="sidebar-label">Overview</div>'
        '<a href="#info">Info</a><a href="#servers">Servers</a></div>'
        f'<div class="sidebar-group"><div class="sidebar-label">Published Messages</div>{links("msg", published)}</div>'
        f'<div class="sidebar-group"><div class="sidebar-label">Consumed Messages</div>{links("msg", consumed)}</div>'
        f'<div class="sidebar-group"><div class="sidebar-label">Schemas</div>{links("schema", sorted(schemas))}</div>'
    )
    main = (
        f'<div class="section" id="info"><div class="section-title">Info</div>'
        f'<div class="info-desc">{esc(_short(str(info.get("description", "")), 800))}</div></div>'
        + _servers(doc.get("servers", {}))
        + '<div class="section" id="messages-published"><div class="section-title">Published Messages</div>'
        + "".join(_message(n, messages[n], schemas) for n in published)
        + '</div><div class="section" id="messages-consumed"><div class="section-title">Consumed Messages</div>'
        + "".join(_message(n, messages[n], schemas) for n in consumed)
        + '</div><div class="section" id="schemas"><div class="section-title">Schemas</div>'
        + "".join(
            _card(
                f"schema-{n}",
                f'<span class="card-title" style="font-family:monospace">{esc(n)}</span>',
                _props_table(schemas[n], n),
            )
            for n in sorted(schemas)
        )
        + "</div>"
    )
    header = (
        f'<div class="header"><div><h1>{esc(info["title"])}</h1>'
        f"<p>Events published to {' and '.join(f'<strong>{esc(t)}</strong>' for t in topics)} "
        "SNS topics</p></div>"
        f'<span class="badge badge-version">v{esc(info["version"])}</span>'
        f'<span class="badge" style="background:linear-gradient(to right,#bd0283,#ec4b3c);color:#fff">'
        f"AsyncAPI {esc(doc['asyncapi'])}</span>"
        f'<span class="badge" style="background:#22c55e;color:#fff;margin-left:auto">{ENV_PLACEHOLDER}</span>'
        '<button id="theme-btn" class="theme-toggle" onclick="toggleTheme()" '
        'title="Toggle light/dark mode">🌙</button></div>'
    )
    top = (
        '<button onclick="window.scrollTo({top:0,behavior:\'smooth\'})" title="Back to top" '
        'style="position:fixed;bottom:24px;right:24px;width:40px;height:40px;'
        "background:linear-gradient(135deg,#8426b0,#bd0283);color:#fff;border:none;border-radius:50%;"
        "font-size:1.1rem;cursor:pointer;box-shadow:0 4px 12px rgba(132,38,176,.4);z-index:100;"
        'display:flex;align-items:center;justify-content:center">↑</button>'
    )
    return (
        '<!DOCTYPE html>\n<html lang="en" data-theme="dark">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{esc(info['title'])} — AsyncAPI</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700'
        '&display=swap" rel="stylesheet">\n'
        f"<style>\n{(HERE / 'asyncapi_page.css').read_text()}</style>\n</head>\n<body>\n"
        f'{header}\n<div class="layout"><nav class="sidebar">{sidebar}</nav>'
        f'<main class="content">{main}</main></div>\n{top}\n'
        f"<script>\n{(HERE / 'asyncapi_page.js').read_text()}</script>\n</body></html>\n"
    )


def _topic(channel: dict[str, Any], doc: dict[str, Any]) -> bool:
    """True for a channel that has a ``send`` operation (an SNS topic this service publishes to)."""
    return any(
        op["action"] == "send" and op["channel"]["$ref"].endswith(f"/{channel['address']}")
        for op in doc["operations"].values()
    )
