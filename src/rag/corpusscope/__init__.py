"""``corpusscope``: scope resolution and every Qdrant filter (LLD §6.2-6.4, DEP-1).

Vendored library: depends on the standard library, ``pydantic`` and ``qdrant_client`` only.
"""

from rag.corpusscope.errors import (
    FilterError,
    ScopeError,
    ScopeExpired,
    ScopeLevelInsufficient,
    ScopeNotFound,
    ScopeStoreUnavailable,
)
from rag.corpusscope.filters import (
    count_filter,
    delete_filter,
    scroll_filter,
    search_filter,
)
from rag.corpusscope.model import (
    PLATFORM,
    REFERENCE,
    TENANT_WIDE,
    CollectionRow,
    Level,
    Principal,
    ScopeRequest,
)
from rag.corpusscope.ports import CollectionStore, ScopeCache, TenderAccess
from rag.corpusscope.qdrant import DenseHit, QdrantScopedReader
from rag.corpusscope.resolver import ScopeResolver
from rag.corpusscope.token import ScopedCollection, ScopeToken

__all__ = [
    "PLATFORM",
    "REFERENCE",
    "TENANT_WIDE",
    "CollectionRow",
    "CollectionStore",
    "DenseHit",
    "FilterError",
    "Level",
    "Principal",
    "QdrantScopedReader",
    "ScopeCache",
    "ScopeError",
    "ScopeExpired",
    "ScopeLevelInsufficient",
    "ScopeNotFound",
    "ScopeRequest",
    "ScopeResolver",
    "ScopeStoreUnavailable",
    "ScopeToken",
    "ScopedCollection",
    "TenderAccess",
    "count_filter",
    "delete_filter",
    "scroll_filter",
    "search_filter",
]
