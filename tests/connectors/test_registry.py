from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

import pytest

from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.registry import (
    all_connectors,
    get_connector,
    register,
)
from tender_monitor.core.schemas import TenderUpsert


def _make_connector_class(name: str) -> type[Connector]:
    class _Conn(Connector):
        source_name: ClassVar[str] = name

        async def _fetch_raw(
            self, since: datetime | None
        ) -> list[dict[str, Any]]:  # pragma: no cover
            return []

        def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:  # pragma: no cover
            raise NotImplementedError

    _Conn.__name__ = f"_Conn_{name}"
    return _Conn


@pytest.mark.usefixtures("fresh_registry")
class TestRegistry:
    def test_register_decorator_registers_class(self) -> None:
        cls = _make_connector_class("alpha")
        register(cls)
        assert get_connector("alpha") is cls

    def test_register_rejects_missing_source_name(self) -> None:
        class _Empty(Connector):
            async def _fetch_raw(
                self, since: datetime | None
            ) -> list[dict[str, Any]]:  # pragma: no cover
                return []

            def _normalize(
                self, raw: dict[str, Any]
            ) -> TenderUpsert:  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(TypeError, match="source_name"):
            register(_Empty)

    def test_register_rejects_duplicate_source_name(self) -> None:
        first = _make_connector_class("dup")
        second = _make_connector_class("dup")

        register(first)
        with pytest.raises(ValueError, match="already registered"):
            register(second)

        # First registration is preserved.
        assert get_connector("dup") is first

    def test_get_connector_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            get_connector("does_not_exist")

    def test_all_connectors_returns_copy(self) -> None:
        cls = _make_connector_class("beta")
        register(cls)

        snapshot = all_connectors()
        snapshot["beta"] = _make_connector_class("beta")  # mutate the copy
        snapshot["gamma"] = _make_connector_class("gamma")  # add to the copy

        assert get_connector("beta") is cls
        assert all_connectors().keys() == {"beta"}
