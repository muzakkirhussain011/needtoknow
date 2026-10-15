"""needtoknow: permission-aware retrieval for RAG.

Every user gets only the chunks they are allowed to read, whatever vector store, permission system
or framework you use.
"""

from needtoknow.authz import AuthorizationError, Authorizer, FunctionAuthorizer, MetadataACLAuthorizer
from needtoknow.backends.memory import InMemoryBackend
from needtoknow.filters import And, Filter, In, Or, matches
from needtoknow.retriever import PermissionedRetriever, RetrievalResult, SearchBackend, Strategy
from needtoknow.types import Document, Hit, Subject

__version__ = "1.0.0.dev0"

__all__ = [
    "And",
    "AuthorizationError",
    "Authorizer",
    "Document",
    "Filter",
    "FunctionAuthorizer",
    "Hit",
    "In",
    "InMemoryBackend",
    "MetadataACLAuthorizer",
    "Or",
    "PermissionedRetriever",
    "RetrievalResult",
    "SearchBackend",
    "Strategy",
    "Subject",
    "__version__",
    "matches",
]
