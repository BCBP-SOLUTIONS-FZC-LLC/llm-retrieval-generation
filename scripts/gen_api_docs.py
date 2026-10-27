"""Generate the committed API specs (the `make swag` / `make asyncapi` equivalents).

    python scripts/gen_api_docs.py            # write docs/swagger/swagger.{json,yaml}, api/asyncapi.{yaml,html}
    python scripts/gen_api_docs.py --check    # exit 1 if either file is stale (CI drift gate)

OpenAPI comes from the FastAPI app, AsyncAPI 3.0 from the event contracts
(``rag.application.events.contracts``), so neither can drift from the code; a test runs the
same check. Run it after changing a route, a request/response model or an event payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling asyncapi_page.py
from asyncapi_page import render  # noqa: E402

from rag.application.events.contracts import (
    CONSUMED,
    INDEX_EVENT_TYPES,
    PUBLISHED,
    SOURCE,
    TOPIC_GENERATION,
    TOPIC_INDEX,
)
from rag.infrastructure.config import Settings
from rag.infrastructure.events.schemas import _adapter, _open
from rag.presentation.main import create_app

ROOT = Path(__file__).resolve().parents[1]
SWAGGER_JSON = ROOT / "docs" / "swagger" / "swagger.json"
SWAGGER_YAML = ROOT / "docs" / "swagger" / "swagger.yaml"
ASYNCAPI = ROOT / "api" / "asyncapi.yaml"
ASYNCAPI_HTML = ROOT / "api" / "asyncapi.html"

# queue -> consumed event types (runtime.QUEUES / §12.4)
QUEUES: dict[str, list[str]] = {
    "llm-document-rag-q": ["DocumentPurged"],
    "domainprofile-llm-invalidate-q": ["DomainProfilePublished"],
    "iam-rag-purge-q": ["TenantMembershipsPurged"],
    "iam-rag-membership-q": ["TenantStateChanged"],
    "tender-rag-q": sorted(
        t for t in CONSUMED if t.startswith("Tender") and not t.startswith("Tenant")
    ),
    "usage-rag-q": ["TenantQuotaRestored"],
}


def openapi_doc() -> dict[str, Any]:
    """OpenAPI 3.1 document of the real app."""
    app = create_app(
        Settings(
            app_name="retrieval-generation",  # not the APP_NAME env (make exports another)
            environment="test",
            enable_tracing=False,
            enable_metrics=False,
        )
    )
    doc = app.openapi()
    # Gateway-injected identity, so Swagger UI's Authorize / Try it out can send it (as in the
    # IAM services' swagger_info.go).
    doc.setdefault("components", {})["securitySchemes"] = {
        "UserID": {
            "type": "apiKey",
            "in": "header",
            "name": "x-user-id",
            "description": "Authenticated user UUID injected by the API gateway.",
        },
        "TenantID": {
            "type": "apiKey",
            "in": "header",
            "name": "x-tenant-id",
            "description": "Tenant UUID injected by the API gateway; sets the RLS scope.",
        },
        "TenantRoles": {
            "type": "apiKey",
            "in": "header",
            "name": "x-tenant-roles",
            "description": "Comma-separated tenant roles injected by the gateway.",
        },
    }
    doc["security"] = [{"UserID": [], "TenantID": [], "TenantRoles": []}]
    return doc


def swagger_json_text() -> str:
    """``swagger.json``, stable for diffing."""
    return json.dumps(openapi_doc(), indent=2, sort_keys=True) + "\n"


def swagger_yaml_text() -> str:
    """``swagger.yaml``: the same document as YAML."""
    return yaml.safe_dump(openapi_doc(), sort_keys=True, allow_unicode=True, width=100)


def _hoist(schema: dict[str, Any], prefix: str, into: dict[str, Any]) -> dict[str, Any]:
    """Move ``$defs`` to ``components.schemas`` and repoint ``#/$defs/X`` refs (no document root)."""
    for name, sub in schema.pop("$defs", {}).items():
        into[f"{prefix}_{name}"] = _hoist(sub, prefix, into)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (
                    f"#/components/schemas/{prefix}_{v.rsplit('/', 1)[1]}"
                    if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/")
                    else walk(v)
                )
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)  # type: ignore[no-any-return]


def asyncapi_doc() -> dict[str, Any]:
    """AsyncAPI 3.0 document: two SNS topics (send) and six SQS queues (receive)."""
    schemas: dict[str, Any] = {}
    messages: dict[str, Any] = {}
    for name in sorted({*PUBLISHED, *CONSUMED}):
        consumed = name not in PUBLISHED
        schema = _adapter(name, consumed).json_schema()
        if not consumed:
            _open(schema)  # LD-65: published payloads stay open
        schema["title"] = name
        schemas[name] = _hoist(schema, name, schemas)
        messages[name] = {
            "name": name,
            "contentType": "application/json",
            "tags": [{"$ref": f"#/components/tags/{'consumed' if consumed else 'published'}"}],
            **(
                {}
                if consumed
                else {"bindings": {"sns": {"messageAttributes": {"EventType": {"value": name}}}}}
            ),
            "title": name,
            "summary": (schema.get("description") or "").strip().splitlines()[0]
            if schema.get("description")
            else name,
            "payload": {"$ref": f"#/components/schemas/{name}"},
        }

    def ref(kind: str, name: str) -> dict[str, str]:
        return {"$ref": f"#/{kind}/{name}"}

    channels: dict[str, Any] = {}
    operations: dict[str, Any] = {}
    topics = {
        TOPIC_GENERATION: sorted(set(PUBLISHED) - INDEX_EVENT_TYPES),
        TOPIC_INDEX: sorted(INDEX_EVENT_TYPES),
    }
    for topic, types in topics.items():
        channels[topic] = {
            "address": topic,
            "description": f"SNS topic `{topic}` (Glue-framed JSON Schema payloads).",
            "messages": {t: ref("components/messages", t) for t in types},
        }
        operations[f"publish-{topic}"] = {
            "action": "send",
            "channel": ref("channels", topic),
            "summary": f"Publish {len(types)} event types to {topic}.",
        }
    for queue, types in QUEUES.items():
        channels[queue] = {
            "address": queue,
            "description": f"SQS queue `{queue}`.",
            "messages": {t: ref("components/messages", t) for t in types},
        }
        operations[f"consume-{queue}"] = {
            "action": "receive",
            "channel": ref("channels", queue),
            "summary": f"Consume {', '.join(types)}.",
        }
    doc = {
        "asyncapi": "3.0.0",
        "info": {
            "title": "LLM Retrieval & Generation Events",
            "version": "1.0.0",
            "description": (
                "Generated by scripts/gen_api_docs.py from rag.application.events.contracts — "
                "do not edit by hand. Every event is an eventcommon Envelope with "
                f"`source: {SOURCE}` and a PascalCase `type` that is also the SNS `EventType` "
                "attribute. Payloads carry identifiers, versions and status only (EVT-7)."
            ),
        },
        "defaultContentType": "application/json",
        "servers": {
            "sns": {
                "host": "sns.eu-west-1.amazonaws.com",
                "protocol": "sns",
                "description": "Publishes to llm-generation-events and llm-index-events.",
            },
            "sqs": {
                "host": "sqs.eu-west-1.amazonaws.com",
                "protocol": "sqs",
                "description": "Consumes the six queues of §12.4 (each with its own DLQ).",
            },
        },
        "channels": channels,
        "operations": operations,
        "components": {
            "messages": messages,
            "schemas": schemas,
            "tags": {
                "published": {"name": "published", "description": "Sent by this service (send)."},
                "consumed": {"name": "consumed", "description": "Received by this service."},
            },
        },
    }
    return doc


def asyncapi_text() -> str:
    """``api/asyncapi.yaml``."""
    return yaml.safe_dump(asyncapi_doc(), sort_keys=True, allow_unicode=True, width=100)


def asyncapi_html() -> str:
    """``api/asyncapi.html``: the themed catalogue page."""
    return render(asyncapi_doc())


def main(argv: list[str]) -> int:
    """Write or ``--check`` both files."""
    outputs = {
        SWAGGER_JSON: swagger_json_text(),
        SWAGGER_YAML: swagger_yaml_text(),
        ASYNCAPI: asyncapi_text(),
        ASYNCAPI_HTML: asyncapi_html(),
    }
    if "--check" in argv:
        stale = [p for p, t in outputs.items() if not p.exists() or p.read_text() != t]
        for p in stale:
            print(f"stale: {p.relative_to(ROOT)} — run `make swag`")
        return 1 if stale else 0
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
