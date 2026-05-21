"""Implementation of ``seed-sources`` — owned by the package so the
CLI can import it. ``scripts/seed_sources.py`` is a thin entry-point
wrapper around ``main()`` here.

Only config-driven fields are written. Runtime counters (last_run_at,
last_success_at, consecutive_failures, last_error, total_tenders_seen)
are owned by the scheduler and explicitly NOT touched here.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tender_monitor.core.database import async_session_factory as default_session_factory
from tender_monitor.core.enums import Country
from tender_monitor.core.models import Source

DEFAULT_PATH = Path("config/sources.yaml")


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    country: Country
    base_url: str
    enabled: bool = True
    schedule_minutes: int = Field(default=60, ge=1)


class SourcesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig] = Field(default_factory=list)


@dataclass(slots=True)
class SeedReport:
    inserted: int
    updated: int
    disabled_present: int


async def seed(
    path: Path,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SeedReport:
    factory = session_factory or default_session_factory
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = SourcesFile.model_validate(data)

    inserted = 0
    updated = 0
    disabled_present = 0

    async with factory() as session:
        for src in config.sources:
            existing = (
                await session.execute(select(Source).where(Source.name == src.name))
            ).scalar_one_or_none()

            stmt = (
                insert(Source)
                .values(
                    name=src.name,
                    display_name=src.display_name,
                    country=src.country,
                    base_url=src.base_url,
                    enabled=src.enabled,
                    schedule_minutes=src.schedule_minutes,
                )
                .on_conflict_do_update(
                    index_elements=[Source.name],
                    set_={
                        "display_name": src.display_name,
                        "country": src.country,
                        "base_url": src.base_url,
                        "enabled": src.enabled,
                        "schedule_minutes": src.schedule_minutes,
                    },
                )
            )
            await session.execute(stmt)

            if existing is None:
                inserted += 1
            else:
                updated += 1
            if not src.enabled:
                disabled_present += 1

        await session.commit()

    return SeedReport(
        inserted=inserted, updated=updated, disabled_present=disabled_present
    )


def main(path: str | Path = DEFAULT_PATH) -> int:
    try:
        report = asyncio.run(seed(Path(path)))
    except FileNotFoundError as exc:
        print(f"file not found: {exc.filename}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"validation failed:\n{exc}", file=sys.stderr)
        return 1
    except yaml.YAMLError as exc:
        print(f"invalid YAML: {exc}", file=sys.stderr)
        return 1

    print(
        f"sources: {report.inserted} inserted, "
        f"{report.updated} updated, "
        f"{report.disabled_present} disabled"
    )
    return 0


__all__ = [
    "DEFAULT_PATH",
    "SeedReport",
    "SourceConfig",
    "SourcesFile",
    "main",
    "seed",
]
