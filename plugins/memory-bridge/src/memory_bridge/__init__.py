"""memory-bridge: universal memory plugin bridge for SWE-ContextBench."""

from .protocol import Memory, MemoryBackend, get_backend, register

__all__ = ["Memory", "MemoryBackend", "get_backend", "register"]
__version__ = "0.1.0"
