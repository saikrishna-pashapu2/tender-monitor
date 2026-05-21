from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tender_monitor.connectors.registry import (
    all_connectors,
    clear_registry,
    register,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def load_json_fixture() -> Callable[[str], Any]:
    """Return a loader for tests/fixtures/<relative_path>."""

    def _load(relative_path: str) -> Any:
        path = FIXTURES_DIR / relative_path
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, object]]]:
    """Capture structlog events emitted inside the test.

    Reset structlog's defaults first so our ``get_logger``'s cached
    BoundLoggers pick up the test capture processor. Mirrors the
    fixture in ``tests/scheduler/conftest.py``; both kept local so
    a future structlog config change only forces an update in one
    of the two scopes that actually rely on log capture.
    """
    import structlog

    structlog.reset_defaults()
    with structlog.testing.capture_logs() as logs:
        yield logs


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """Snapshot the registry, empty it, and restore on teardown.

    Tests register synthetic Connector subclasses; without this fixture
    they would leak across tests and trip the duplicate-registration
    check. We restore real connectors (auto-registered at package
    import) afterwards so subsequent tests still see them.
    """
    snapshot = all_connectors()
    clear_registry()
    try:
        yield
    finally:
        clear_registry()
        for cls in snapshot.values():
            register(cls)
