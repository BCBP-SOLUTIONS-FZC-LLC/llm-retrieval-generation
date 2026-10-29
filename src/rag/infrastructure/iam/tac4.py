"""TAC-4 client: the overlay grant for ``(tenant, tender, principal)`` (§4.7.2, §22.5)."""

from __future__ import annotations

from uuid import UUID

import httpx
from fastapicommon.logging import get_logger
from fastapicommon.propagation import create_platform_http_client

log = get_logger("rag.tac4")
TAC4_PATH = "/internal/tenants/{tenant}/tenders/{tender}/acl/{user}"


class HttpTenderAccess:
    """``corpusscope.TenderAccess`` over IAM's Tender ACL service.

    Body per ``iam-lld-tender-acl-service`` §8.4 (TAC-4): ``{"has_access": true, "access_level":
    "view"|"edit"|"approve", "expires_at": ...}``, or ``{"has_access": false, "access_level":
    null}`` for a successful no-access answer, which IAM never sends as a 404 (CG-5). Anything
    else, including a non-200, raises, so the resolver refuses the request (CG-4).
    """

    def __init__(self, base_url: str, timeout_seconds: float = 0.1) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds

    async def level(self, tenant_id: UUID, tender_id: UUID, principal_id: UUID) -> str | None:
        """The held level label, or ``None``."""
        path = TAC4_PATH.format(tenant=tenant_id, tender=tender_id, user=principal_id)
        async with create_platform_http_client(
            base_url=self._base_url, timeout=self._timeout
        ) as client:
            response = await client.get(path)
            if response.status_code != 200:
                log.warning("tac4_refused", status=response.status_code)
                raise httpx.HTTPStatusError(
                    "TAC-4 refused", request=response.request, response=response
                )
            body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("has_access"), bool):
            raise ValueError("TAC-4 has_access is not a boolean")
        if not body["has_access"]:
            return None
        level = body.get("access_level")
        if not isinstance(level, str):
            raise ValueError("TAC-4 access_level is not a string")
        return level


class UnconfiguredTenderAccess:
    """No IAM URL configured: a request naming a restricted tender is refused (CG-4)."""

    async def level(self, tenant_id: UUID, tender_id: UUID, principal_id: UUID) -> str | None:
        """Always raises."""
        raise RuntimeError("TAC-4 is not configured")
