"""Search backends. Each adapter for an external store lives in its own module and optional extra."""

from needtoknow.backends.memory import InMemoryBackend

__all__ = ["InMemoryBackend"]
