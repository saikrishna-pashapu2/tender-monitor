"""One-off DB migration: rename source 'goszakup' to 'zakup_unified'.

The original connector was named 'goszakup' but actually hit
zakup.gov.kz (the unified procurement portal). After Prompt 12
introduced an HTML scraper for the real goszakup.gov.kz, the API-based
one was renamed to 'zakup_unified' so the new connector could claim
the correct name.

This script:
  1. Inserts a new 'zakup_unified' row in sources, copying the runtime
     counters (last_run_at, last_success_at, consecutive_failures,
     last_error, total_tenders_seen, schedule_minutes, etc.) from the
     existing 'goszakup' row so the next scheduler tick treats the
     handoff as continuous.
  2. Updates every tenders.source_name = 'goszakup' to 'zakup_unified'.
  3. Deletes the old 'goszakup' sources row.

After this script runs, the operator should also run
``tender-monitor seed-sources`` to refresh the display_name and any
other config-driven fields from sources.yaml.

Idempotent: re-running once already migrated is a no-op and prints
"already migrated".

Run from project root:

    .venv/Scripts/python.exe scripts/rename_goszakup_to_zakup_unified.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select, text, update

from tender_monitor.core.database import async_session_factory
from tender_monitor.core.models import Source, Tender

OLD_NAME = "goszakup"
NEW_NAME = "zakup_unified"


async def migrate() -> int:
    async with async_session_factory() as session:
        old = (
            await session.execute(select(Source).where(Source.name == OLD_NAME))
        ).scalar_one_or_none()
        new = (
            await session.execute(select(Source).where(Source.name == NEW_NAME))
        ).scalar_one_or_none()

        if old is None and new is not None:
            # Already migrated.
            tender_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM tenders WHERE source_name = :n"
                    ),
                    {"n": NEW_NAME},
                )
            ).scalar_one()
            print(
                f"already migrated: {NEW_NAME!r} present "
                f"({tender_count} tenders), {OLD_NAME!r} not present. "
                "no changes."
            )
            return 0

        if old is None and new is None:
            print(
                f"nothing to migrate: neither {OLD_NAME!r} nor "
                f"{NEW_NAME!r} exist in sources."
            )
            return 0

        if old is not None and new is not None:
            print(
                f"both {OLD_NAME!r} and {NEW_NAME!r} exist in sources. "
                "refusing to merge automatically; resolve manually.",
                file=sys.stderr,
            )
            return 1

        # Happy path: old present, new absent. Insert NEW carrying the
        # OLD counters; bulk-update tender source_name; delete OLD.
        assert old is not None  # mypy
        new_source = Source(
            name=NEW_NAME,
            display_name=old.display_name,
            country=old.country,
            base_url=old.base_url,
            enabled=old.enabled,
            schedule_minutes=old.schedule_minutes,
            last_run_at=old.last_run_at,
            last_success_at=old.last_success_at,
            consecutive_failures=old.consecutive_failures,
            last_error=old.last_error,
            total_tenders_seen=old.total_tenders_seen,
        )
        session.add(new_source)
        await session.flush()

        result = await session.execute(
            update(Tender)
            .where(Tender.source_name == OLD_NAME)
            .values(source_name=NEW_NAME)
        )
        tenders_updated = result.rowcount or 0

        await session.delete(old)
        await session.commit()

        print(
            f"migrated: inserted {NEW_NAME!r} (counters copied from "
            f"{OLD_NAME!r}), updated {tenders_updated} tenders, "
            f"deleted {OLD_NAME!r}."
        )
        print(
            "next step: run `tender-monitor seed-sources` to refresh "
            "the display_name and any other config-driven fields."
        )
        return 0


def main() -> int:
    return asyncio.run(migrate())


if __name__ == "__main__":
    sys.exit(main())
