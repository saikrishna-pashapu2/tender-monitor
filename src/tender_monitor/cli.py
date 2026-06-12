from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
import truststore
import yaml
from pydantic import ValidationError
from sqlalchemy import select

# ets-tender.kz (and any future portal) sometimes serves a leaf cert
# whose intermediate isn't in certifi's bundle. truststore routes
# Python's SSL through the OS cert store (the same one curl uses on
# Windows), which transparently fetches missing intermediates via AIA.
# Idempotent; safe to call once per process and must happen BEFORE
# any module imports httpx (httpx caches the SSL context internally).
truststore.inject_into_ssl()

from tender_monitor.connectors import (  # noqa: E402  (must follow truststore inject)
    FetchError,
    all_connectors,
    get_connector,
)
from tender_monitor.core.database import (  # noqa: E402
    async_session_factory,
    dispose_engine,
)
from tender_monitor.core.logging import configure_logging  # noqa: E402
from tender_monitor.core.models import Tender  # noqa: E402
from tender_monitor.core.schemas import TenderUpsert  # noqa: E402
from tender_monitor.matching import (  # noqa: E402
    KeywordsConfig,
    match_tender,
)
from tender_monitor.matching import match_text as run_match_text  # noqa: E402
from tender_monitor.scheduler import Runner, ingest_source  # noqa: E402
from tender_monitor.scheduler.seed import DEFAULT_PATH as DEFAULT_SOURCES_PATH  # noqa: E402
from tender_monitor.scheduler.seed import main as seed_main  # noqa: E402

DEFAULT_KEYWORDS_PATH = Path("config/keywords.yaml")


def _ensure_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 if they're TextIOWrappers.

    Connectors emit Cyrillic titles by design. The default Windows
    console uses cp1252, which crashes on these characters. Reconfigure
    only when we can; on platforms or test harnesses where stdout is
    not a TextIOWrapper this is a no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


@click.group()
def cli() -> None:
    """tender-monitor command-line interface."""
    _ensure_utf8_streams()


@cli.command("run-scheduler")
def run_scheduler() -> None:
    """Run the scheduler: one ingest job per enabled source, forever.

    Configures structured logging, installs SIGINT/SIGTERM handlers,
    and runs an immediate first ingest for every enabled source so the
    operator doesn't have to wait for the first interval.
    """
    configure_logging()
    runner = Runner()
    # On Windows, Ctrl+C raises KeyboardInterrupt instead of firing
    # through add_signal_handler; the finally inside run_forever still
    # runs Runner.stop, so this is a clean exit.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run_forever())


@cli.command("run-notifier")
def run_notifier() -> None:
    """Run the notifier process (Telegram + email)."""
    click.echo("not implemented yet")


@cli.command("run-api")
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind address.")
@click.option("--port", default=8000, show_default=True, type=int, help="Listen port.")
def run_api(host: str, port: int) -> None:
    """Run the FastAPI service for the internal portal."""
    import uvicorn  # local import to keep CLI import time cheap

    configure_logging()
    uvicorn.run(
        "tender_monitor.api.app:app",
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )


@cli.command("list-connectors")
def list_connectors() -> None:
    """List every connector currently registered in the process."""
    registry = all_connectors()
    if not registry:
        click.echo("no connectors registered")
        return

    rows = [
        (name, cls.__name__, cls.__module__)
        for name, cls in sorted(registry.items())
    ]
    name_w = max(len("source_name"), max(len(r[0]) for r in rows))
    cls_w = max(len("class"), max(len(r[1]) for r in rows))
    mod_w = max(len("module"), max(len(r[2]) for r in rows))

    header = f"{'source_name':<{name_w}}  {'class':<{cls_w}}  {'module':<{mod_w}}"
    click.echo(header)
    click.echo("-" * len(header))
    for name, cls_name, module in rows:
        click.echo(f"{name:<{name_w}}  {cls_name:<{cls_w}}  {module:<{mod_w}}")


@cli.command("run-connector")
@click.argument("source_name")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only fetch items published on or after this date (YYYY-MM-DD).",
)
@click.option(
    "--limit",
    type=int,
    default=5,
    show_default=True,
    help="How many tenders to print as JSON; the connector still fetches everything.",
)
def run_connector(source_name: str, since: datetime | None, limit: int) -> None:
    """Run a single connector once and print the result.

    Does not write to the database — the scheduler is the only thing that
    persists tenders. Use this command to smoke-test a connector against
    a saved fixture or live source.
    """
    try:
        connector_cls = get_connector(source_name)
    except KeyError:
        available = sorted(all_connectors().keys())
        listed = ", ".join(available) if available else "<none>"
        click.echo(
            f"unknown source '{source_name}'; available: {listed}", err=True
        )
        sys.exit(1)

    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    connector = connector_cls()
    result = asyncio.run(connector.fetch_latest(since=since))

    click.echo(
        f"source={result.source_name} "
        f"raw={result.raw_item_count} "
        f"normalized={len(result.tenders)} "
        f"partial_errors={len(result.partial_errors)} "
        f"duration_ms={result.duration_ms:.1f}"
    )

    if result.partial_errors:
        click.echo("partial errors:", err=True)
        for err in result.partial_errors:
            click.echo(f"  - {err}", err=True)

    for tender in result.tenders[:limit]:
        click.echo(json.dumps(tender.model_dump(mode="json"), ensure_ascii=False))


@cli.command("match-text")
@click.argument("text")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_KEYWORDS_PATH,
    show_default=True,
    help="Path to the keywords YAML file.",
)
def match_text_cmd(text: str, path: Path) -> None:
    """Run the matcher on a single string and print which groups fired."""
    config = KeywordsConfig.load(path)
    result = run_match_text(text, config)
    if not result.is_match:
        click.echo("no match")
        return

    click.echo(f"matched groups: {', '.join(result.matched_groups)}")
    for group in result.matched_groups:
        details = result.match_details[group]
        click.echo(f"  {group}:")
        if details["matched_phrases"]:
            click.echo(
                f"    phrases: {', '.join(repr(p) for p in details['matched_phrases'])}"
            )
        if details["matched_tokens"]:
            click.echo(
                f"    tokens:  {', '.join(repr(t) for t in details['matched_tokens'])}"
            )


@cli.command("validate-keywords")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_KEYWORDS_PATH,
    show_default=True,
    help="Path to the keywords YAML file.",
)
def validate_keywords_cmd(path: Path) -> None:
    """Validate the keywords YAML and report group/phrase/token counts."""
    try:
        config = KeywordsConfig.load(path)
    except FileNotFoundError as exc:
        click.echo(f"file not found: {exc.filename}", err=True)
        sys.exit(1)
    except yaml.YAMLError as exc:
        click.echo(f"invalid YAML: {exc}", err=True)
        sys.exit(1)
    except ValidationError as exc:
        click.echo("validation failed:", err=True)
        click.echo(str(exc), err=True)
        sys.exit(1)

    total_phrases = sum(len(g.phrases) for g in config.groups.values())
    total_tokens = sum(len(g.tokens) for g in config.groups.values())
    total_excludes = sum(
        len(g.exclude_if_contains) for g in config.groups.values()
    )

    click.echo(f"groups: {len(config.groups)}")
    click.echo(f"phrases (total): {total_phrases}")
    click.echo(f"tokens (total): {total_tokens}")
    click.echo(f"excludes (total): {total_excludes}")
    for name, group in config.groups.items():
        click.echo(
            f"  {name}: {len(group.phrases)} phrases, "
            f"{len(group.tokens)} tokens, "
            f"{len(group.exclude_if_contains)} excludes"
        )


def _tender_to_upsert(row: Tender) -> TenderUpsert:
    return TenderUpsert(
        source_name=row.source_name,
        external_id=row.external_id,
        title=row.title,
        title_en=row.title_en,
        title_language=row.title_language,
        translation_provider=row.translation_provider,
        title_translated_at=row.title_translated_at,
        buyer_name=row.buyer_name,
        buyer_external_id=row.buyer_external_id,
        country=row.country,
        sector=row.sector,
        value_amount=row.value_amount,
        value_currency=row.value_currency,
        published_at=row.published_at,
        deadline_at=row.deadline_at,
        status=row.status,
        source_url=row.source_url,
        language=row.language,
        raw_json=row.raw_json,
    )


async def _rematch_existing(
    *,
    path: Path,
    source_names: tuple[str, ...],
    dry_run: bool,
) -> dict[str, int]:
    config = KeywordsConfig.load(path)
    changed = 0
    cleared = 0
    matched = 0
    total = 0

    async with async_session_factory() as session:
        stmt = select(Tender).order_by(Tender.source_name, Tender.external_id)
        if source_names:
            stmt = stmt.where(Tender.source_name.in_(source_names))

        rows = (await session.execute(stmt)).scalars().all()
        total = len(rows)
        for row in rows:
            previous_groups = list(row.matched_groups or [])
            previous_details = row.match_details
            result = match_tender(_tender_to_upsert(row), config)
            match_details = result.match_details if result.match_details else None

            if result.matched_groups:
                matched += 1
            if (
                previous_groups != result.matched_groups
                or previous_details != match_details
            ):
                changed += 1
                if previous_groups and not result.matched_groups:
                    cleared += 1
                row.matched_groups = list(result.matched_groups)
                row.match_details = match_details

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    await dispose_engine()
    return {
        "total": total,
        "matched": matched,
        "changed": changed,
        "cleared": cleared,
    }


@cli.command("rematch-existing")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_KEYWORDS_PATH,
    show_default=True,
    help="Path to the keywords YAML file.",
)
@click.option(
    "--source",
    "source_names",
    multiple=True,
    help="Restrict rematching to one source. Can be passed more than once.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Compute changes but roll them back.",
)
def rematch_existing_cmd(
    path: Path,
    source_names: tuple[str, ...],
    dry_run: bool,
) -> None:
    """Recompute stored matches for tenders already in the database."""
    result = asyncio.run(
        _rematch_existing(path=path, source_names=source_names, dry_run=dry_run)
    )
    click.echo(
        f"tenders={result['total']} "
        f"matched={result['matched']} "
        f"changed={result['changed']} "
        f"cleared={result['cleared']} "
        f"dry_run={str(dry_run).lower()}"
    )


@cli.command("seed-sources")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_SOURCES_PATH,
    show_default=True,
    help="Path to the sources YAML file.",
)
def seed_sources_cmd(path: Path) -> None:
    """Upsert sources into the DB from the YAML config.

    Only config-driven fields are touched; runtime counters owned by
    the scheduler (last_run_at, consecutive_failures, etc.) are left
    alone, so this is safe to re-run.
    """
    sys.exit(seed_main(path))


@cli.command("run-once")
@click.argument("source_name")
def run_once_cmd(source_name: str) -> None:
    """Run a single ingest now, persisting tenders to the DB.

    Use this for ad-hoc fetches or smoke-testing a connector against
    real storage. The scheduler runs the same code on a cron, but this
    command is a useful way to validate a source's behavior without
    waiting for the next tick.
    """
    configure_logging()
    try:
        result = asyncio.run(ingest_source(source_name))
    except LookupError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except FetchError as exc:
        click.echo(f"ingest failed: {exc}", err=True)
        sys.exit(1)

    if result.skipped:
        click.echo(f"source={source_name} skipped (disabled)")
        return

    click.echo(
        f"source={result.source_name} "
        f"fetched={result.fetched} "
        f"normalized={result.normalized} "
        f"created={result.created} "
        f"updated={result.updated} "
        f"unchanged={result.unchanged} "
        f"matched={result.matched} "
        f"partial_errors={len(result.partial_errors)} "
        f"duration_ms={result.duration_ms:.1f}"
    )
    if result.partial_errors:
        click.echo("partial errors:", err=True)
        for err in result.partial_errors:
            click.echo(f"  - {err}", err=True)


if __name__ == "__main__":
    cli()
