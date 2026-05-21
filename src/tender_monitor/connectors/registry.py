from __future__ import annotations

from tender_monitor.connectors.base import Connector

_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    """Class decorator. Registers ``cls`` under ``cls.source_name``.

    Raises:
        TypeError: if the class has no class-level ``source_name``.
        ValueError: if the source name is already registered. We refuse
            to overwrite to avoid silent collisions when two modules
            forget about each other.
    """
    source_name = getattr(cls, "source_name", None)
    if not source_name:
        raise TypeError(
            f"{cls.__name__} cannot be registered without a non-empty "
            "class-level `source_name`."
        )
    if source_name in _REGISTRY:
        existing = _REGISTRY[source_name]
        raise ValueError(
            f"source_name '{source_name}' already registered to "
            f"{existing.__module__}.{existing.__name__}; refusing to "
            f"overwrite with {cls.__module__}.{cls.__name__}."
        )
    _REGISTRY[source_name] = cls
    return cls


def get_connector(source_name: str) -> type[Connector]:
    """Return the registered connector class for ``source_name``.

    Raises KeyError if not registered.
    """
    return _REGISTRY[source_name]


def all_connectors() -> dict[str, type[Connector]]:
    """Return a copy of the registry.

    The copy is intentional so callers can iterate or mutate the result
    without affecting the live registry.
    """
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Empty the registry. Test-only helper; do not call from production."""
    _REGISTRY.clear()


__all__ = [
    "all_connectors",
    "clear_registry",
    "get_connector",
    "register",
]
