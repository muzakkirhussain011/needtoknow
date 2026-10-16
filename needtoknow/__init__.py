"""needtoknow: permission-aware retrieval for RAG.

Every user gets only the chunks they are allowed to read, whatever vector store, permission system
or framework you use.
"""

from needtoknow.authz import AuthorizationError, Authorizer, FunctionAuthorizer, MetadataACLAuthorizer
from needtoknow.backends.memory import InMemoryBackend
from needtoknow.caching import CachingAuthorizer
from needtoknow.events import JsonLinesAudit, RetrievalEvent
from needtoknow.filters import And, Filter, In, Or, filter_from_dict, filter_to_dict, matches
from needtoknow.retriever import PermissionedRetriever, RetrievalResult, SearchBackend, Strategy
from needtoknow.types import Document, Hit, Subject

__version__ = "1.1.2"

__all__ = [
    "And",
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
    "RetrievalEvent",
    "RetrievalResult",
    "SearchBackend",
    "Strategy",
    "Subject",
    "__version__",
    "filter_from_dict",
    "filter_to_dict",
    "matches",
]
