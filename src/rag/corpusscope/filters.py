"""Every Qdrant filter of the platform is built here, from a ``ScopeToken`` (SCOPE-1, MD-08).

Search, count, scroll and delete share one mandatory compound (SCOPE-2): ``tenant_id``,
``tender_id``, ``collection_id``, ``index_generation``. Search and count add ``is_current``
(RET-8), which no caller can relax. A filter is per collection, because each collection has
its own physical collection, shard key and generation (SCOPE-5).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from qdrant_client import models

from rag.corpusscope.errors import FilterError
from rag.corpusscope.token import ScopedCollection, ScopeToken


def _match(key: str, value: str | int | bool) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def mandatory_conditions(token: ScopeToken, collection: ScopedCollection) -> list[models.Condition]:
    """The four mandatory terms. Raises where the collection is empty (SCOPE-12) or foreign."""
    if not isinstance(token, ScopeToken):  # runtime backstop for the type rule (ISO-3)
        raise TypeError("filters are built from a ScopeToken only")
    if collection.index_generation is None:
        raise FilterError(f"collection {collection.id} has no active index generation")
    if token.collection(collection.id) != collection:
        raise FilterError(f"collection {collection.id} is not the token's")
    # Payload uuids are strings compared byte for byte: canonical lower case (§4.10).
    return [
        _match("tenant_id", str(collection.tenant_id)),
        _match("tender_id", str(collection.tender_id)),
        _match("collection_id", str(collection.id)),
        _match("index_generation", collection.index_generation),
    ]


def _live(token: ScopeToken, collection_id: UUID, now: datetime) -> ScopedCollection:
    if not isinstance(token, ScopeToken):  # runtime backstop for the type rule (ISO-3)
        raise TypeError("filters are built from a ScopeToken only")
    token.require_live(now)
    return token.collection(collection_id)


def search_filter(
    token: ScopeToken,
    collection_id: UUID,
    *,
    now: datetime,
    document_roles: Sequence[str] = (),
    language: str | None = None,
    entity_type: str | None = None,
) -> models.Filter:
    """Dense-search filter: mandatory terms + ``is_current`` + optional structural predicates.

    ``language`` matches on the primary subtag (§4.10): ``en`` reaches ``en-GB``. This assumes
    Ingestion indexes ``language`` as a word-tokenised text field (assumption, §20).
    """
    collection = _live(token, collection_id, now)
    must = mandatory_conditions(token, collection)
    must.append(_match("is_current", True))
    if document_roles:
        must.append(
            models.FieldCondition(
                key="document_role", match=models.MatchAny(any=list(document_roles))
            )
        )
    if language:
        primary = language.split("-")[0].lower()
        must.append(models.FieldCondition(key="language", match=models.MatchText(text=primary)))
    if entity_type:
        must.append(_match("entity_types", entity_type))
    return models.Filter(must=must)


def count_filter(
    token: ScopeToken,
    collection_id: UUID,
    *,
    now: datetime,
    entity_type: str | None = None,
    document_role: str | None = None,
) -> models.Filter:
    """Required-input count (§6.9): mandatory terms + ``is_current`` + the entity type or role."""
    collection = _live(token, collection_id, now)
    must = mandatory_conditions(token, collection)
    must.append(_match("is_current", True))
    if entity_type:
        must.append(_match("entity_types", entity_type))
    if document_role:
        must.append(_match("document_role", document_role))
    return models.Filter(must=must)


def delete_filter(
    token: ScopeToken, collection_id: UUID, *, now: datetime, document_id: UUID | None = None
) -> models.Filter:
    """Delete-by-filter for the ingestion deployables (SCOPE-1 covers deletes, MD-08 widened).

    No ``is_current`` term: a delete must reach superseded points too.
    """
    collection = _live(token, collection_id, now)
    must = mandatory_conditions(token, collection)
    if document_id is not None:
        must.append(_match("document_id", str(document_id)))
    return models.Filter(must=must)


def scroll_filter(token: ScopeToken, collection_id: UUID, *, now: datetime) -> models.Filter:
    """Scroll filter: the mandatory terms alone (no ``is_current``: audit/rebuild readers)."""
    return models.Filter(must=mandatory_conditions(token, _live(token, collection_id, now)))
