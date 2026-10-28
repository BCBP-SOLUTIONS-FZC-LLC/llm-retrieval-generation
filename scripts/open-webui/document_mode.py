"""Create the "document-editor" model preset in Open WebUI (idempotent).

A preset is a named model with a system prompt, built on one of the gateway's task classes. This
one makes the model answer with the whole document as a single ``html`` block every time, which
Open WebUI renders in its Artifacts side panel, so an edit request ("shorten the intro") shows the
revised document beside the chat. Run by ``make chat-up``; safe to run again.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("OPEN_WEBUI_URL", "http://localhost:3000")
BASE_MODEL = os.environ.get("DOCUMENT_BASE_MODEL", "draft")  # a task class the gateway serves
PRESET_ID = "document-editor"
SYSTEM = """You are a document editor. The user gives you a document or asks you to write one, \
then asks for changes.

Rules:
- Always reply with the COMPLETE current document inside ONE ```html code block: a self-contained \
HTML fragment (headings, paragraphs, lists, tables).
- When editing, copy every part the user did not mention exactly as it is: the title, every \
heading and every other section stay in the output. Change only the part the user named.
- Never reply with only the changed part. Never split the document across several blocks.
- After the code block, add at most one short sentence saying what you changed.
- If the request is unclear, ask one question and do not output a block.

Example. Document: <h1>Plan</h1><h2>Goals</h2><p>Ship it.</p><h2>Risks</h2><p>Late. Costly.</p>
Request: "Make Risks a bullet list."
Reply:
```html
<h1>Plan</h1>
<h2>Goals</h2>
<p>Ship it.</p>
<h2>Risks</h2>
<ul><li>Late.</li><li>Costly.</li></ul>
```
Made Risks a bullet list."""


def call(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    # WEBUI_AUTH=False: the local single-user account signs in with the built-in credentials
    token = call(
        "POST", "/api/v1/auths/signin", body={"email": "admin@localhost", "password": "admin"}
    )["token"]
    preset = {
        "id": PRESET_ID,
        "base_model_id": BASE_MODEL,
        "name": "Document editor",
        "meta": {
            "description": "Edits a document in the side panel: reply is always the whole document.",
            "capabilities": {"citations": False},
        },
        "params": {"system": SYSTEM, "temperature": 0.1},
        "is_active": True,
    }
    try:
        call("POST", f"/api/v1/models/model/update?id={PRESET_ID}", token, preset)
        print(f"updated preset {PRESET_ID!r} on {BASE_MODEL!r}")
    except urllib.error.HTTPError:  # not there yet (Open WebUI answers 401 for an unknown id)
        call("POST", "/api/v1/models/create", token, preset)
        print(f"created preset {PRESET_ID!r} on {BASE_MODEL!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
