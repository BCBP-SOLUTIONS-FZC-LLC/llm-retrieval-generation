"""Domain Profile and Language Pack resolution against Tender (§22.3, §22.9, LD-14, §14.3).

Tender is the pack authority and we pull. A hit in the Valkey cache never leaves the process
(RET-3). A miss calls Tender with a 2 s timeout and two retries with backoff behind a breaker.
Open breaker or a failure after the retries raises ``PackUnavailable``; the caller falls back
to the platform default profile with a marker. Assumed route:
``GET /internal/packs/resolve?domain_profile_ref=&language=&version=`` with ``x-tenant-id``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
from fastapicommon.logging import get_logger
from fastapicommon.propagation import create_platform_http_client

from rag.application.retrieval.ports import KeyValueCache
from rag.domain.packs.profile import PackBundle, PackCache, PackUnavailable
from rag.infrastructure.resilience import BreakerOpen, CircuitBreaker
from rag.infrastructure.tender.pack_codec import bundle_from_dict, bundle_to_dict

log = get_logger("rag.packs")
PACK_PATH = "/internal/packs/resolve"
TIMEOUT_SECONDS = 2.0
BACKOFF_SECONDS = (0.1, 0.3)


def identity_of(domain_profile_ref: str | None, language: str | None) -> str:
    """The pack identity half of the cache key (``{tenant}:pack:{identity}:{version}``)."""
    return f"{domain_profile_ref or '-'}:{language or '-'}"


class HttpPackResolver:
    """``PackResolver`` over Tender, cache-fronted by a ``PackCache``."""

    def __init__(
        self,
        base_url: str,
        cache: PackCache | None,
        *,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
    ) -> None:
        self._base_url, self._cache, self._backoff = base_url, cache, backoff
        self._breaker = CircuitBreaker(name="tender_packs")
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()

    async def resolve(
        self,
        tenant_id: UUID,
        *,
        domain_profile_ref: str | None = None,
        language: str | None = None,
        pinned_version: str | None = None,
    ) -> PackBundle:
        """The cached bundle, else Tender's. Raises ``PackUnavailable``."""
        identity = identity_of(domain_profile_ref, language)
        if self._cache is not None:
            hit = await self._cache.get(tenant_id, identity, pinned_version)
            if hit is not None:
                return hit
        body = await self._fetch(tenant_id, domain_profile_ref, language, pinned_version)
        try:
            bundle = bundle_from_dict(body)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise PackUnavailable("Tender returned an unreadable pack") from exc
        if self._cache is not None:
            await self._cache.put(tenant_id, identity, pinned_version, bundle)
        return bundle

    async def _fetch(
        self, tenant_id: UUID, ref: str | None, language: str | None, version: str | None
    ) -> dict[str, Any]:
        if self._client is None:
            self._client = create_platform_http_client(base_url=self._base_url)
        client = self._client
        params = {
            k: v
            for k, v in {
                "domain_profile_ref": ref,
                "language": language,
                "version": version,
            }.items()
            if v
        }

        async def once() -> dict[str, Any]:
            response = await client.get(
                PACK_PATH,
                params=params,
                headers={"x-tenant-id": str(tenant_id)},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("pack body is not an object")
            return data

        async def with_retries() -> dict[str, Any]:
            for delay in (*self._backoff, None):
                try:
                    return await once()
                except httpx.HTTPError, ValueError:
                    if delay is None:
                        raise
                    await asyncio.sleep(delay)
            raise AssertionError("unreachable")

        try:
            return await self._breaker.call(with_retries)
        except (BreakerOpen, httpx.HTTPError, ValueError) as exc:
            log.warning("pack_resolution_failed", reason=type(exc).__name__)
            raise PackUnavailable("Tender cannot resolve the pack") from exc


class ValkeyPackCache:
    """``PackCache`` over a string cache: ``{tenant}:pack:{identity}:{version}``, 1 h (§11.1).

    A bundle is stored under the requested version (``current`` when none) and under its own
    version, so a run's pinned read hits after any earlier read of that version.
    """

    def __init__(self, cache: KeyValueCache, ttl_seconds: int = 3600) -> None:
        self._cache, self._ttl = cache, ttl_seconds

    @staticmethod
    def _key(tenant_id: UUID, identity: str, version: str | None) -> str:
        return f"{tenant_id}:pack:{identity}:{version or 'current'}"

    async def get(self, tenant_id: UUID, identity: str, version: str | None) -> PackBundle | None:
        """The cached bundle; a corrupt entry is a miss."""
        raw = await self._cache.get(self._key(tenant_id, identity, version))
        if raw is None:
            return None
        try:
            return bundle_from_dict(json.loads(raw))
        except ValueError, KeyError, TypeError, AttributeError:
            return None

    async def put(
        self, tenant_id: UUID, identity: str, version: str | None, bundle: PackBundle
    ) -> None:
        """Store under the requested and the bundle's own version."""
        raw = json.dumps(bundle_to_dict(bundle))
        own = bundle.domain_profile.version
        for v in {version, own}:
            await self._cache.set(self._key(tenant_id, identity, v), raw, self._ttl)
