"""Stage 1 (§6.1): pack resolution with the platform-default fallback (LD-14)."""

from __future__ import annotations

from uuid import UUID

from rag.application.packs.ports import PackResolver
from rag.application.retrieval.ports import NullObserver, PipelineObserver
from rag.domain.packs.profile import PLATFORM_DEFAULT_BUNDLE, PackBundle, PackUnavailable
from rag.domain.retrieval.plan import DegradationMarker


async def resolve_or_default(
    resolver: PackResolver,
    tenant_id: UUID,
    observer: PipelineObserver | None = None,
    *,
    domain_profile_ref: str | None = None,
    language: str | None = None,
    pinned_version: str | None = None,
) -> tuple[PackBundle, list[DegradationMarker]]:
    """The bundle, and a marker where Tender could not answer and the default was used.

    Falls back only when Tender is unavailable (breaker open, or a failure after the retries).
    """
    obs = observer or NullObserver()
    try:
        bundle = await resolver.resolve(
            tenant_id,
            domain_profile_ref=domain_profile_ref,
            language=language,
            pinned_version=pinned_version,
        )
        return bundle, []
    except PackUnavailable:
        obs.marker("pack", "tender_unavailable")
        return PLATFORM_DEFAULT_BUNDLE, [
            DegradationMarker("pack", "tender_unavailable", "platform_default")
        ]
