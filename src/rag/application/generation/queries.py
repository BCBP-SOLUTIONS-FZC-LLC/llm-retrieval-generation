"""Read use cases: A-1 to A-4, A-8, A-9 and R-5.

Every read re-resolves scope on the caller's current grants (API-10): possessing an id
authorizes nothing. The artefact is read first in its own short transaction because its
``tender_id`` is what the scope check needs; the scope check runs with no transaction open.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from fastapicommon.logging import get_logger

from rag.application.generation.dtos import (
    ArtefactDTO,
    PageDTO,
    RetrievalSetDTO,
    SectionContentDTO,
    SectionStateDTO,
    VersionDetailDTO,
    VersionSummaryDTO,
)
from rag.application.generation.mappers import (
    artefact_dto,
    retrieval_set_dto,
    section_state_dto,
    version_detail_dto,
    version_summary_dto,
)
from rag.application.generation.ports import ArtefactContentStore
from rag.application.shared.pagination import clamp_window
from rag.application.shared.ports import TransactionRunner
from rag.application.shared.scope import ScopeAuthorizer, ScopeTarget
from rag.domain.generation.artefact import Artefact
from rag.domain.generation.repositories import ArtefactReadRepository, RetrievalSetRepository
from rag.domain.shared.enums import GrantLevel
from rag.domain.shared.errors import DomainError, ErrorCode
from rag.domain.shared.identity import RequestIdentity

log = get_logger(__name__)

CONTENT_TYPE = "text/markdown"


def _not_found() -> DomainError:
    return DomainError(ErrorCode.ARTEFACT_NOT_FOUND, "The artefact was not found.")


@dataclass(frozen=True, slots=True)
class _ArtefactReader:
    """Shared prologue: load the artefact (404), then authorize at ``answer`` (API-10)."""

    tx: TransactionRunner
    repo: ArtefactReadRepository
    scope: ScopeAuthorizer

    async def authorized(self, identity: RequestIdentity, artefact_id: UUID) -> Artefact:
        artefact = await self.tx.run_in_tx(
            lambda tx: self.repo.get_artefact(tx, artefact_id), read_only=True
        )
        if artefact is None:
            raise _not_found()
        try:
            await self.scope.require_level(
                identity, ScopeTarget(tender_id=artefact.tender_id), GrantLevel.ANSWER
            )
        except DomainError as exc:
            if exc.code is ErrorCode.COLLECTION_NOT_FOUND:
                raise _not_found() from exc  # CG-8: revoked looks exactly like absent
            raise
        return artefact


class GetArtefact(_ArtefactReader):
    """A-1: the artefact with its current version, tier and eligibility verdict."""

    async def execute(self, identity: RequestIdentity, artefact_id: UUID) -> ArtefactDTO:
        """Return the artefact or ``404 ARTEFACT_NOT_FOUND``."""
        await self.authorized(identity, artefact_id)
        view = await self.tx.run_in_tx(
            lambda tx: self.repo.get_artefact_view(tx, artefact_id), read_only=True
        )
        if view is None:
            raise _not_found()
        return artefact_dto(view)


class ListVersions(_ArtefactReader):
    """A-2: version history, newest first, paginated; no reports (API-8)."""

    async def execute(
        self,
        identity: RequestIdentity,
        artefact_id: UUID,
        limit: int | None = None,
        offset: int | None = None,
    ) -> PageDTO[VersionSummaryDTO]:
        """A page of version summaries; ``limit`` above 500 is clamped, not rejected."""
        window = clamp_window(limit, offset)
        await self.authorized(identity, artefact_id)
        items, total = await self.tx.run_in_tx(
            lambda tx: self.repo.list_versions(
                tx, artefact_id, limit=window.limit, offset=window.offset
            ),
            read_only=True,
        )
        return PageDTO([version_summary_dto(v) for v in items], total, window.limit, window.offset)


class GetVersion(_ArtefactReader):
    """A-3: one version with per-section groundedness, completeness and retrieval set refs."""

    async def execute(
        self, identity: RequestIdentity, artefact_id: UUID, version_no: int
    ) -> VersionDetailDTO:
        """The version in full, or ``404 ARTEFACT_NOT_FOUND``. A low tier withholds nothing."""
        await self.authorized(identity, artefact_id)
        detail = await self.tx.run_in_tx(
            lambda tx: self.repo.get_version_detail(tx, artefact_id, version_no), read_only=True
        )
        if detail is None:
            raise _not_found()
        return version_detail_dto(detail)


class ListSections(_ArtefactReader):
    """A-4: section states and per-section tiers, paginated."""

    async def execute(
        self,
        identity: RequestIdentity,
        artefact_id: UUID,
        limit: int | None = None,
        offset: int | None = None,
    ) -> PageDTO[SectionStateDTO]:
        """A page of sections ordered by ordinal."""
        window = clamp_window(limit, offset)
        await self.authorized(identity, artefact_id)
        items, total = await self.tx.run_in_tx(
            lambda tx: self.repo.list_sections(
                tx, artefact_id, limit=window.limit, offset=window.offset
            ),
            read_only=True,
        )
        return PageDTO([section_state_dto(s) for s in items], total, window.limit, window.offset)


@dataclass(frozen=True, slots=True)
class GetSectionVersionContent:
    """A-9: the generated text of one section version, read from S3 through ``content_ref``."""

    tx: TransactionRunner
    repo: ArtefactReadRepository
    scope: ScopeAuthorizer
    store: ArtefactContentStore

    async def execute(
        self, identity: RequestIdentity, artefact_id: UUID, section_ref: str, version_no: int
    ) -> SectionContentDTO:
        """The text with its retrieval set id and tier.

        A section with no such version (``gap`` / ``failed``) is ``404 ARTEFACT_NOT_FOUND``; an
        unreadable object, or one whose bytes no longer hash to ``content_sha256`` (ASV-4), is
        ``503 ARTEFACT_CONTENT_UNAVAILABLE``.
        """
        await _ArtefactReader(self.tx, self.repo, self.scope).authorized(identity, artefact_id)
        record = await self.tx.run_in_tx(
            lambda tx: self.repo.get_section_version_record(
                tx, artefact_id, section_ref, version_no
            ),
            read_only=True,
        )
        if record is None:
            raise _not_found()
        sv = record.section_version
        data = await self.store.get(sv.tenant_id, sv.content_ref)
        if hashlib.sha256(data).digest() != sv.content_sha256:
            log.error("section_version_content_digest_mismatch", section_version=str(sv.id))
            raise DomainError(
                ErrorCode.ARTEFACT_CONTENT_UNAVAILABLE, "The section content is unavailable."
            )
        return SectionContentDTO(
            section_version_id=sv.id,
            version_no=sv.version_no,
            origin=sv.origin.value,
            content_type=CONTENT_TYPE,
            content=data.decode("utf-8"),
            retrieval_set_id=sv.retrieval_set_id,
            evidence_tier=sv.evidence_tier.value,
        )


class RenderArtefact(_ArtefactReader):
    """A-8: reserved (S-6). Scope and artefact first, then ``503 RENDERER_UNAVAILABLE``."""

    async def execute(self, identity: RequestIdentity, artefact_id: UUID) -> None:
        """Always raises: a 404 would wrongly suggest the version is missing."""
        await self.authorized(identity, artefact_id)
        raise DomainError(ErrorCode.RENDERER_UNAVAILABLE, "No renderer is available.")


@dataclass(frozen=True, slots=True)
class GetRetrievalSet:
    """R-5: the recorded set with chunks, filters, markers and purge rows (``search`` level)."""

    tx: TransactionRunner
    repo: RetrievalSetRepository
    scope: ScopeAuthorizer

    async def execute(self, identity: RequestIdentity, retrieval_set_id: UUID) -> RetrievalSetDTO:
        """The set, or ``404 RETRIEVAL_SET_NOT_FOUND`` where absent, reclaimed or out of scope.

        The set's excerpts are verbatim source text, so scope is re-resolved on every read: a
        caller whose grant was revoked since generation gets the same 404 as an absent set.
        """
        view = await self.tx.run_in_tx(
            lambda tx: self.repo.get_view(tx, retrieval_set_id), read_only=True
        )
        not_found = DomainError(
            ErrorCode.RETRIEVAL_SET_NOT_FOUND, "The retrieval set was not found."
        )
        if view is None:
            raise not_found
        collections = {*view.collection_ids, *_filter_collection_ids(view.retrieval_set.filter_set)}
        try:
            await self.scope.require_level(
                identity,
                ScopeTarget(view.retrieval_set.tender_id, tuple(sorted(collections))),
                GrantLevel.SEARCH,
            )
        except DomainError as exc:
            if exc.code in (ErrorCode.GRANT_LEVEL_INSUFFICIENT, ErrorCode.COLLECTION_NOT_FOUND):
                raise not_found from exc  # revoked or no longer active: same 404 as absent
            raise
        return retrieval_set_dto(view)


def _filter_collection_ids(filter_set: dict[str, object]) -> set[UUID]:
    """Collection ids the set's recorded filter named; unparseable entries are ignored."""
    raw = filter_set.get("collection_ids")
    found: set[UUID] = set()
    for item in raw if isinstance(raw, list) else []:
        try:
            found.add(UUID(str(item)))
        except ValueError:
            continue
    return found
