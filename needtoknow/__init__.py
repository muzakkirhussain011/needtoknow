"""needtoknow: permission-aware retrieval for RAG.

Every user gets only the chunks they are allowed to read, whatever vector store, permission system
or framework you use.
"""

from needtoknow.authz import (
    AllOf,
    AnyOf,
    AuthorizationError,
    Authorizer,
    FunctionAuthorizer,
    MetadataACLAuthorizer,
    TenantAuthorizer,
)
from needtoknow.backends.memory import InMemoryBackend
from needtoknow.caching import CachingAuthorizer
from needtoknow.events import JsonLinesAudit, RetrievalEvent
from needtoknow.filters import And, Filter, In, Or, filter_from_dict, filter_to_dict, matches
from needtoknow.fusion import RankFusion
from needtoknow.retriever import PermissionedRetriever, Reranker, RetrievalResult, SearchBackend, Strategy
from needtoknow.types import Document, Hit, Query, Subject

__version__ = "1.3.2"

__all__ = [
    "AllOf",
    "And",
    "AnyOf",
    "AuthorizationError",
    "Authorizer",
    "CachingAuthorizer",
    "Document",
    "Filter",
    "FunctionAuthorizer",
    "Hit",
    "In",
    "InMemoryBackend",
    "JsonLinesAudit",
    "MetadataACLAuthorizer",
    "Or",
    "PermissionedRetriever",
    "Query",
    "RankFusion",
    "Reranker",
    "RetrievalEvent",
    "RetrievalResult",
    "SearchBackend",
    "Strategy",
    "Subject",
    "TenantAuthorizer",
    "__version__",
    "filter_from_dict",
    "filter_to_dict",
    "matches",
]
