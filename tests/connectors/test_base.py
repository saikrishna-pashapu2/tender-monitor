from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

import pytest

from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.core.enums import Country
from tender_monitor.core.schemas import TenderUpsert


class _FakeConnector(Connector):
    """Reads tests/fixtures/_fake/listing.json and normalizes each item.

    Items missing ``id`` raise ParseError so the partial-error path can be
    exercised by tests.
    """

    source_name: ClassVar[str] = "_fake"

    def __init__(self, items: list[dict[str, Any]]) -> None:
        super().__init__()
        self._items = items

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        return list(self._items)

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id = raw.get("id")
        if not external_id:
            raise ParseError("missing id")
        return TenderUpsert(
            source_name=self.source_name,
            external_id=external_id,
            title=raw["title_ru"],
            buyer_name=raw.get("buyer"),
            country=Country.KZ,
            value_amount=raw.get("amount"),
            value_currency=raw.get("currency"),
            source_url=raw["url"],
            raw_json=raw,
        )


class _ExplodingConnector(Connector):
    source_name: ClassVar[str] = "_exploding"

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        raise FetchError("upstream is down")

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:  # pragma: no cover
        raise NotImplementedError


async def test_fetch_latest_happy_path(
    load_json_fixture: Callable[[str], Any],
) -> None:
    payload = load_json_fixture("_fake/listing.json")
    items = [item for item in payload["items"] if "id" in item]
    assert len(items) == 4  # the malformed item is filtered out for this test

    connector = _FakeConnector(items)
    result = await connector.fetch_latest()

    assert result.source_name == "_fake"
    assert result.raw_item_count == 4
    assert len(result.tenders) == 4
    assert result.partial_errors == []
    assert result.duration_ms > 0
    assert result.fetched_at.tzinfo is not None
    # Sanity-check that normalized fields are populated
    assert {t.external_id for t in result.tenders} == {
        "fake-1",
        "fake-2",
        "fake-3",
        "fake-5",
    }


async def test_fetch_latest_partial_normalize_error(
    load_json_fixture: Callable[[str], Any],
) -> None:
    payload = load_json_fixture("_fake/listing.json")
    items = payload["items"]
    assert len(items) == 5  # one item is malformed

    connector = _FakeConnector(items)
    result = await connector.fetch_latest()

    assert result.raw_item_count == 5
    assert len(result.tenders) == 4
    assert len(result.partial_errors) == 1
    assert "ParseError" in result.partial_errors[0]


async def test_fetch_raw_failure_propagates() -> None:
    connector = _ExplodingConnector()
    with pytest.raises(FetchError):
        await connector.fetch_latest()


class _HintProbeConnector(Connector):
    """Records what ``self._known_external_ids`` looked like during
    ``_fetch_raw`` so tests can assert the hint plumbing without
    needing a real HTTP transport.
    """

    source_name: ClassVar[str] = "_hint_probe"

    def __init__(self) -> None:
        super().__init__()
        self.seen_hint_inside_fetch: set[str] | None | object = "unset"

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        # Snapshot the value the base class stashed for this call.
        self.seen_hint_inside_fetch = self._known_external_ids
        return []

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:  # pragma: no cover
        raise NotImplementedError


async def test_fetch_latest_stashes_known_external_ids_on_self() -> None:
    connector = _HintProbeConnector()
    hint = {"abc", "def"}
    await connector.fetch_latest(known_external_ids=hint)
    assert connector.seen_hint_inside_fetch == hint


async def test_fetch_latest_clears_known_external_ids_after_call() -> None:
    connector = _HintProbeConnector()
    await connector.fetch_latest(known_external_ids={"abc", "def"})
    # After the call the attribute is back to None so a subsequent
    # run with no hint doesn't accidentally see the previous value.
    assert connector._known_external_ids is None


async def test_fetch_latest_known_external_ids_defaults_to_none() -> None:
    connector = _HintProbeConnector()
    await connector.fetch_latest()
    # Inside _fetch_raw the value was None (no hint supplied).
    assert connector.seen_hint_inside_fetch is None
    # And still None afterwards.
    assert connector._known_external_ids is None


async def test_fetch_latest_clears_hint_even_when_fetch_raises() -> None:
    class _ExplodesAfterStash(Connector):
        source_name: ClassVar[str] = "_explode_after_stash"
        seen: ClassVar[set[str] | None | object] = "unset"

        async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
            type(self).seen = self._known_external_ids
            raise FetchError("boom")

        def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:  # pragma: no cover
            raise NotImplementedError

    connector = _ExplodesAfterStash()
    with pytest.raises(FetchError):
        await connector.fetch_latest(known_external_ids={"x"})
    # The finally clause ran even though _fetch_raw raised.
    assert connector._known_external_ids is None


def test_subclass_must_set_source_name() -> None:
    """Documented behavior: missing source_name raises at instantiation
    time, not class-definition time. This lets us define abstract
    intermediate base classes (e.g. a future shared XML connector) that
    don't carry their own source name.
    """

    class _NoName(Connector):
        async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
            return []

        def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(TypeError, match="source_name"):
        _NoName()
