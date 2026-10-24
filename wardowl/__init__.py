"""wardowl: permission-aware retrieval for RAG.

Every user gets only the chunks they are allowed to read, whatever vector store, permission system
or framework you use.
"""

from wardowl.authz import (
    AllOf,
    AnyOf,
    AuthorizationError,
    Authorizer,
    FunctionAuthorizer,
    MetadataACLAuthorizer,
    TenantAuthorizer,
)
from wardowl.backends.memory import InMemoryBackend
from wardowl.caching import CachingAuthorizer
from wardowl.events import JsonLinesAudit, RetrievalEvent
from wardowl.filters import And, Filter, In, Or, filter_from_dict, filter_to_dict, matches
from wardowl.fusion import RankFusion
from wardowl.redaction import PatternRedactor, Redactor, SpanRedactor
from wardowl.retriever import PermissionedRetriever, Reranker, RetrievalResult, SearchBackend, Strategy
from wardowl.types import Document, Hit, Query, Subject

__version__ = "2.0.0"

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
    "PatternRedactor",
    "PermissionedRetriever",
    "Query",
    "RankFusion",
    "Redactor",
    "Reranker",
    "RetrievalEvent",
    "RetrievalResult",
    "SearchBackend",
    "SpanRedactor",
    "Strategy",
    "Subject",
    "TenantAuthorizer",
    "__version__",
    "filter_from_dict",
    "filter_to_dict",
    "matches",
]
