"""Regeneration rules (LLD §10.1, §5.7.9): REGEN-1 to REGEN-9 as pure functions."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from uuid import UUID

from rag.domain.generation.artefact import Artefact
from rag.domain.generation.section import ArtefactSection
from rag.domain.shared.enums import ArtefactKind, ArtefactStatus
from rag.domain.shared.errors import DomainError, ErrorCode


class RegenerationScope(StrEnum):
    """A-6 regenerates a section, A-5 a document."""

    SECTION = "section"
    DOCUMENT = "document"


def mints_document_version(scope: RegenerationScope) -> bool:
    """REGEN-8: A-5 is an explicit request for a document version, A-6 mints a section version."""
    return scope is RegenerationScope.DOCUMENT


def stale(detail: str) -> DomainError:
    """``409 OPTIMISTIC_LOCK_CONFLICT``: the caller regenerates from a version it has not seen."""
    return DomainError(ErrorCode.OPTIMISTIC_LOCK_CONFLICT, detail)


def guard_document_base(artefact: Artefact, base_version_id: UUID) -> None:
    """REGEN-2 for A-5: ``base_version_id`` is the artefact's current document version."""
    if artefact.current_version_id is None or artefact.current_version_id != base_version_id:
        raise stale("base_version_id is not the artefact's current version.")


def guard_section_base(
    artefact: Artefact,
    section: ArtefactSection,
    base_version_id: UUID,
    *,
    document_holds_current_section_version: bool,
) -> None:
    """REGEN-2 for A-6.

    The base is the section's current *section* version, or the artefact's current *document*
    version provided that document version still holds the section's current section version
    (a section edit supersedes it, so a second reviewer on the old reading is refused). A section
    with no version yet (``gap``, ``failed``) can only be based on the current document version.
    The LLD does not say which kind of id A-6 takes; Tender sees both (A-2, A-3), so both work.
    """
    if section.current_version_id is not None and base_version_id == section.current_version_id:
        return
    if artefact.current_version_id == base_version_id and (
        section.current_version_id is None or document_holds_current_section_version
    ):
        return
    raise stale("base_version_id is not the section's current version.")


def require_regenerable(artefact: Artefact) -> None:
    """A regeneration needs an artefact at rest: a run in flight owns it.

    ``complete``, ``failed`` and ``cancelled`` may go ``generating`` again (ART-5). ``pending`` and
    ``generating`` mean a run is live. An archived artefact refuses new work (ART-7).
    """
    artefact.require_writable()
    # §24 gives A-5 and A-6 ``OPTIMISTIC_LOCK_CONFLICT`` as their 409 (ERR-1: no other state code):
    # a run in flight means the caller's base version is no longer the one to regenerate from.
    if artefact.status in (ArtefactStatus.PENDING, ArtefactStatus.GENERATING):
        raise stale("A run is already in flight for this artefact.")
    if artefact.status is ArtefactStatus.SUPERSEDED:  # reserved (ART-5): nothing writes it
        raise stale("The artefact is superseded.")


def long_form(artefact: Artefact) -> bool:
    """REGEN-4: only a long-form document has stage 1, so only it has established facts."""
    return artefact.kind is ArtefactKind.DOCUMENT


def fact_conflict_possible(artefact: Artefact, established_fact_count: int) -> bool:
    """REGEN-4: only a long-form artefact with stage-1 facts can conflict; a draft has none."""
    return long_form(artefact) and established_fact_count > 0


def scope_covers(
    prior_collection_ids: Iterable[UUID], resolved_collection_ids: Iterable[UUID]
) -> bool:
    """REGEN-5: the current scope still reaches every collection a retrieval set drew from."""
    return set(prior_collection_ids) <= set(resolved_collection_ids)
