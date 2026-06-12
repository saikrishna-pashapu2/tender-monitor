"""Per-source ingest orchestration.

One ``ingest_source(source_name)`` call = one connector run +
match-every-tender + persist + source-health update. The scheduler
(runner.py) calls this on a cadence; the CLI's ``run-once`` calls it
directly.

Failure model: connector exceptions are recorded on the Source row in
a separate session (so the failure metadata survives even when the
main ingest transaction rolls back) and then re-raised. The scheduler
catches the re-raise at the job boundary so one bad source can't kill
the others.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tender_monitor.connectors import (
    Connector,
    FetchError,
    get_connector,
)
from tender_monitor.core.config import settings
from tender_monitor.core.database import async_session_factory as default_session_factory
from tender_monitor.core.logging import get_logger
from tender_monitor.core.models import Source, Tender
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.matching import KeywordsConfig, MatchResult, match_tender
from tender_monitor.notifications import dispatch_many
from tender_monitor.scheduler.upsert import UpsertOutcome, upsert_tender
from tender_monitor.translation import (
    TitleTranslation,
    TitleTranslator,
    TranslationError,
    build_title_translator,
)

logger = get_logger(__name__)

DEFAULT_BACKFILL = timedelta(days=7)
DEFAULT_KEYWORDS_PATH = Path("config/keywords.yaml")
# How far back the scheduler looks when building the
# ``known_external_ids`` hint that connectors use to skip
# already-processed tenders. 14 days is generous enough to cover
# typical KZ/UZ tender deadlines (<30 days) plus a few days of
# post-deadline listing inertia, while keeping the set bounded
# (~2-3k IDs/source -- trivial memory). Module-level so it's easy
# to tune later without threading a config knob through.
KNOWN_IDS_LOOKBACK_DAYS = 14
_T = TypeVar("_T")


@dataclass(slots=True)
class IngestResult:
    source_name: str
    fetched: int
    normalized: int
    created: int
    updated: int
    unchanged: int
    matched: int
    partial_errors: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class _StoredTitleTranslation:
    title: str
    title_en: str | None
    title_language: str | None
    translation_provider: str | None
    title_translated_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _chunked(items: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@lru_cache(maxsize=4)
def _load_keywords_cached(path: str) -> KeywordsConfig:
    return KeywordsConfig.load(path)


def reset_keywords_cache() -> None:
    """Drop the cached KeywordsConfig — useful in tests and after edits."""
    _load_keywords_cached.cache_clear()


async def _load_source(session: AsyncSession, name: str) -> Source | None:
    return (
        await session.execute(select(Source).where(Source.name == name))
    ).scalar_one_or_none()


async def _load_known_external_ids(
    session: AsyncSession, source_name: str, *, now: datetime
) -> set[str]:
    """Return the set of external IDs this source has seen recently.

    "Recently" means ``last_seen_at`` within the last
    ``KNOWN_IDS_LOOKBACK_DAYS``. Connectors use this hint to skip
    expensive per-tender work (e.g. national_bank's detail fetches)
    for tenders we've already processed. An empty set is a valid
    answer: it means "we've seen nothing for this source recently",
    which is functionally the same as no hint for any connector
    that consults it.
    """
    cutoff = now - timedelta(days=KNOWN_IDS_LOOKBACK_DAYS)
    result = await session.execute(
        select(Tender.external_id)
        .where(Tender.source_name == source_name)
        .where(Tender.last_seen_at > cutoff)
    )
    return set(result.scalars().all())


async def _load_stored_title_translations(
    session: AsyncSession, source_name: str, external_ids: Sequence[str]
) -> dict[str, _StoredTitleTranslation]:
    if not external_ids:
        return {}

    result = await session.execute(
        select(
            Tender.external_id,
            Tender.title,
            Tender.title_en,
            Tender.title_language,
            Tender.translation_provider,
            Tender.title_translated_at,
        )
        .where(Tender.source_name == source_name)
        .where(Tender.external_id.in_(set(external_ids)))
    )
    return {
        row.external_id: _StoredTitleTranslation(
            title=row.title,
            title_en=row.title_en,
            title_language=row.title_language,
            translation_provider=row.translation_provider,
            title_translated_at=row.title_translated_at,
        )
        for row in result
    }


def _apply_title_translation(
    upsert: TenderUpsert,
    translation: TitleTranslation,
    *,
    provider: str,
    translated_at: datetime,
) -> TenderUpsert:
    translated = translation.text.strip()
    if not translated:
        return upsert
    return upsert.model_copy(
        update={
            "title_en": translated,
            "title_language": translation.detected_language or upsert.language.value,
            "translation_provider": provider,
            "title_translated_at": translated_at,
        }
    )


def _reuse_stored_title_translation(
    upsert: TenderUpsert, stored: _StoredTitleTranslation
) -> TenderUpsert:
    return upsert.model_copy(
        update={
            "title_en": stored.title_en,
            "title_language": stored.title_language,
            "translation_provider": stored.translation_provider,
            "title_translated_at": stored.title_translated_at,
        }
    )


async def _translate_tender_titles(
    tenders: Sequence[TenderUpsert],
    *,
    translator: TitleTranslator | None,
    stored_translations: dict[str, _StoredTitleTranslation],
    translated_at: datetime,
) -> list[TenderUpsert]:
    if translator is None or not tenders:
        return list(tenders)

    enriched = list(tenders)
    pending: list[tuple[int, TenderUpsert]] = []
    reused = 0

    for index, upsert in enumerate(enriched):
        stored = stored_translations.get(upsert.external_id)
        if stored and stored.title == upsert.title and stored.title_en:
            enriched[index] = _reuse_stored_title_translation(upsert, stored)
            reused += 1
            continue
        pending.append((index, upsert))

    translated = 0
    for batch in _chunked(pending, settings.translation_batch_size):
        texts = [upsert.title for _, upsert in batch]
        try:
            translations = await translator.translate_titles(texts, source_language="auto")
        except TranslationError as exc:
            logger.warning(
                "scheduler.translation.failed",
                provider=translator.provider,
                count=len(texts),
                error=str(exc),
            )
            continue
        except Exception as exc:
            logger.warning(
                "scheduler.translation.failed",
                provider=translator.provider,
                count=len(texts),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue

        for (index, upsert), translation in zip(batch, translations, strict=False):
            enriched[index] = _apply_title_translation(
                upsert,
                translation,
                provider=translator.provider,
                translated_at=translated_at,
            )
            translated += 1

    if reused or translated:
        logger.info(
            "scheduler.translation.complete",
            provider=translator.provider,
            reused=reused,
            translated=translated,
            skipped=len(pending) - translated,
        )
    return enriched


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    source_name: str,
    exc: BaseException,
) -> None:
    """Bump consecutive_failures + record last_error in a fresh session.

    Lives in its own transaction so the failure metadata is durable
    even if the main ingest session has been rolled back.
    """
    async with session_factory() as session:
        source = await _load_source(session, source_name)
        if source is None:
            return
        source.consecutive_failures += 1
        source.last_error = f"{type(exc).__name__}: {exc}"
        await session.commit()


async def ingest_source(
    source_name: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: Callable[[], datetime] = _utcnow,
    keywords_path: Path | str = DEFAULT_KEYWORDS_PATH,
    title_translator: TitleTranslator | None = None,
) -> IngestResult:
    factory = session_factory or default_session_factory
    run_started_at = now()

    # --- Setup: load source, capture previous cursor, mark this run as started.
    async with factory() as session:
        source = await _load_source(session, source_name)
        if source is None:
            raise LookupError(
                f"source {source_name!r} not found in sources table; "
                "run `tender-monitor seed-sources` first"
            )
        if not source.enabled:
            logger.warning(
                "scheduler.ingest.skipped_disabled",
                source=source_name,
            )
            return IngestResult(
                source_name=source_name,
                fetched=0,
                normalized=0,
                created=0,
                updated=0,
                unchanged=0,
                matched=0,
                skipped=True,
            )

        previous_last_run_at = source.last_run_at
        source.last_run_at = run_started_at
        await session.commit()

    since = previous_last_run_at or (run_started_at - DEFAULT_BACKFILL)

    logger.info(
        "scheduler.ingest.start",
        source=source_name,
        since=since.isoformat(),
    )

    # --- Build the known-IDs hint from the DB.
    async with factory() as session:
        known_ids = await _load_known_external_ids(
            session, source_name, now=run_started_at
        )
    logger.debug(
        "scheduler.known_ids_loaded",
        source=source_name,
        count=len(known_ids),
    )

    # --- Run connector.
    connector_cls: type[Connector] = get_connector(source_name)
    connector = connector_cls()
    try:
        fetch_result = await connector.fetch_latest(
            since=since, known_external_ids=known_ids
        )
    except Exception as exc:
        await _record_failure(factory, source_name, exc)
        logger.error(
            "scheduler.ingest.failed",
            source=source_name,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # Re-raise so the scheduler / CLI sees the failure too.
        if isinstance(exc, FetchError):
            raise
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    translator = title_translator if title_translator is not None else build_title_translator()
    async with factory() as session:
        stored_translations = await _load_stored_title_translations(
            session,
            source_name,
            [upsert.external_id for upsert in fetch_result.tenders],
        )
    fetch_result.tenders = await _translate_tender_titles(
        fetch_result.tenders,
        translator=translator,
        stored_translations=stored_translations,
        translated_at=run_started_at,
    )

    # --- Match + upsert in one transaction.
    keywords_config = _load_keywords_cached(str(keywords_path))
    counts: dict[UpsertOutcome, int] = {
        UpsertOutcome.created: 0,
        UpsertOutcome.updated: 0,
        UpsertOutcome.unchanged: 0,
    }
    matched_count = 0
    # IDs of rows that were freshly *created* AND matched ≥ 1 group on
    # this run. These are the only tenders that trigger an outbound
    # email — re-matches (updates) deliberately don't, otherwise a
    # keyword YAML tweak would spam every recipient about every old
    # tender they already saw.
    new_matched_ids: list[UUID] = []

    async with factory() as session:
        for upsert in fetch_result.tenders:
            try:
                match = match_tender(upsert, keywords_config)
            except Exception as exc:
                # Defensive: a buggy matcher must NEVER cost us a tender row.
                logger.error(
                    "scheduler.matcher_failed",
                    source=source_name,
                    external_id=upsert.external_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                match = MatchResult()

            outcome = await upsert_tender(session, upsert, match, run_started_at)
            counts[outcome.outcome] += 1
            if match.is_match:
                matched_count += 1
            if outcome.outcome is UpsertOutcome.created and match.is_match:
                new_matched_ids.append(outcome.tender_id)

            logger.debug(
                "scheduler.tender.upsert",
                source=source_name,
                external_id=upsert.external_id,
                outcome=outcome.outcome.value,
            )
            if outcome.outcome is UpsertOutcome.updated:
                logger.info(
                    "scheduler.tender.changed",
                    source=source_name,
                    external_id=upsert.external_id,
                    tender_id=str(outcome.tender_id),
                    fields=outcome.changes,
                )

        # Success metadata on the same transaction as the tenders.
        source = await _load_source(session, source_name)
        assert source is not None  # we loaded it above and won't have lost the row
        source.last_success_at = run_started_at
        source.consecutive_failures = 0
        source.last_error = None
        source.total_tenders_seen += fetch_result.raw_item_count

        await session.commit()

    # --- Fire-and-forget email dispatch. Runs AFTER the upsert commit so a
    # failed SMTP call can never roll back ingested rows.
    if new_matched_ids:
        try:
            sent = await dispatch_many(
                session_factory=factory, tender_ids=new_matched_ids
            )
            logger.info(
                "scheduler.notifications.dispatched",
                source=source_name,
                new_matched=len(new_matched_ids),
                emails_sent=sent,
            )
        except Exception as exc:
            logger.error(
                "scheduler.notifications.failed",
                source=source_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    result = IngestResult(
        source_name=source_name,
        fetched=fetch_result.raw_item_count,
        normalized=len(fetch_result.tenders),
        created=counts[UpsertOutcome.created],
        updated=counts[UpsertOutcome.updated],
        unchanged=counts[UpsertOutcome.unchanged],
        matched=matched_count,
        partial_errors=list(fetch_result.partial_errors),
        duration_ms=fetch_result.duration_ms,
    )
    logger.info(
        "scheduler.ingest.complete",
        source=source_name,
        fetched=result.fetched,
        normalized=result.normalized,
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        matched=result.matched,
        partial_errors=len(result.partial_errors),
        duration_ms=result.duration_ms,
    )
    return result


__all__ = [
    "DEFAULT_BACKFILL",
    "IngestResult",
    "ingest_source",
    "reset_keywords_cache",
]
