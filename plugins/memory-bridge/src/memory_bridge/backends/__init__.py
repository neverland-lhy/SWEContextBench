"""Backend adapters (the only swappable layer).

Importing this package registers all built-in backends via @register.
"""

from . import trivial, mem0, custom  # noqa: F401


def load() -> None:
    """No-op hook; importing this module is enough. Exists so callers can
    force registration without an explicit import elsewhere."""
    return
