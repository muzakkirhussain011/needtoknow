"""Search backends. Each adapter for an external store lives in its own module and optional extra."""

from wardowl.backends._errors import NothingMatches
from wardowl.backends.memory import InMemoryBackend

__all__ = ["InMemoryBackend", "NothingMatches"]
