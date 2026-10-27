"""Artefact read routes: A-1 to A-4, A-8, A-9 (LLD 5.4, 5.7.8, 5.7.11).

Authentication is declared per route (API-11). Scope is re-resolved by each use case on
every read (API-10); a tenant id in a path or body is never the authority (RLS-2).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from rag.domain.shared.identity import RequestIdentity
from rag.presentation.api.public.artefacts_models import (
    ArtefactResponse,
    SectionContentResponse,
    SectionPage,
    VersionDetailResponse,
    VersionPage,
)
from rag.presentation.generation_dependencies import GenerationServices, get_generation
from rag.presentation.identity import require_identity

router = APIRouter(prefix="/api/v1/artefacts", tags=["artefacts"])

Identity = Annotated[RequestIdentity, Depends(require_identity)]
Services = Annotated[GenerationServices, Depends(get_generation)]
Limit = Annotated[int | None, Query(ge=1, description="Default 100; above 500 is clamped.")]
Offset = Annotated[int | None, Query(ge=0)]


@router.get("/{artefact_id}", response_model=ArtefactResponse)
async def get_artefact(
    artefact_id: UUID, identity: Identity, services: Services
) -> ArtefactResponse:
    """A-1: the artefact with its current version, tier and eligibility verdict."""
    return ArtefactResponse.model_validate(
        await services.get_artefact.execute(identity, artefact_id)
    )


@router.get("/{artefact_id}/versions", response_model=VersionPage)
async def list_versions(
    artefact_id: UUID,
    identity: Identity,
    services: Services,
    limit: Limit = None,
    offset: Offset = None,
) -> VersionPage:
    """A-2: version history, newest first."""
    page = await services.list_versions.execute(identity, artefact_id, limit, offset)
    return VersionPage.model_validate(page)


@router.get("/{artefact_id}/versions/{version_no}", response_model=VersionDetailResponse)
async def get_version(
    artefact_id: UUID,
    version_no: Annotated[int, Path(ge=1)],
    identity: Identity,
    services: Services,
) -> VersionDetailResponse:
    """A-3: one version with per-section groundedness, completeness and retrieval set refs."""
    detail = await services.get_version.execute(identity, artefact_id, version_no)
    return VersionDetailResponse.model_validate(detail)


@router.get("/{artefact_id}/sections", response_model=SectionPage)
async def list_sections(
    artefact_id: UUID,
    identity: Identity,
    services: Services,
    limit: Limit = None,
    offset: Offset = None,
) -> SectionPage:
    """A-4: section states and per-section evidence tiers."""
    page = await services.list_sections.execute(identity, artefact_id, limit, offset)
    return SectionPage.model_validate(page)


@router.get("/{artefact_id}/render")
async def render_artefact(
    artefact_id: UUID,
    identity: Identity,
    services: Services,
    version_no: Annotated[int | None, Query(ge=1)] = None,
    format: Annotated[str | None, Query(max_length=32)] = None,  # noqa: A002 - the wire name
) -> None:
    """A-8: reserved. ``503 RENDERER_UNAVAILABLE`` after scope and artefact checks (S-6)."""
    await services.render_artefact.execute(identity, artefact_id)


@router.get(
    "/{artefact_id}/sections/{section_ref}/versions/{version_no}/content",
    response_model=SectionContentResponse,
)
async def get_section_content(
    artefact_id: UUID,
    section_ref: Annotated[str, Path(max_length=256)],
    version_no: Annotated[int, Path(ge=1)],
    identity: Identity,
    services: Services,
) -> SectionContentResponse:
    """A-9: the generated text of one section version."""
    content = await services.get_section_content.execute(
        identity, artefact_id, section_ref, version_no
    )
    return SectionContentResponse.model_validate(content)
