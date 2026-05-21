"""One-shot diagnostic: does zakup.gov.kz (system_id__in=1__2__3) already
consolidate MITWORK and Samruk-Kazyna? Read-only — runs five aggregate
queries over the existing `tenders` table and prints the results.

Run from the project root:

    python scripts/inspect_consolidation.py
"""

from __future__ import annotations

import asyncio
import io
import sys

from sqlalchemy import text

from tender_monitor.core.database import async_session_factory


def _ensure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


QUERIES: list[tuple[str, str]] = [
    (
        "1. System distribution (system_id, system_name, count)",
        """
        SELECT raw_json -> 'system' ->> 'id'   AS system_id,
               raw_json -> 'system' ->> 'name' AS system_name,
               count(*)                        AS n
        FROM tenders
        WHERE source_name = 'goszakup'
        GROUP BY 1, 2
        ORDER BY n DESC;
        """,
    ),
    (
        "2. National Bank presence",
        """
        SELECT count(*) AS national_bank_tenders
        FROM tenders
        WHERE source_name = 'goszakup'
          AND (buyer_name ILIKE '%национальный банк%'
               OR buyer_name ILIKE '%ұлттық банк%'
               OR buyer_name ILIKE '%national bank%');
        """,
    ),
    (
        "3. Samruk-Kazyna presence",
        """
        SELECT count(*)                        AS samruk_tenders,
               count(DISTINCT buyer_name)      AS distinct_samruk_buyers
        FROM tenders
        WHERE source_name = 'goszakup'
          AND (buyer_name ILIKE '%самрук%'
               OR buyer_name ILIKE '%samruk%'
               OR raw_json -> 'organizer' ->> 'iin_bin' LIKE '081040%');
        """,
    ),
    (
        "4. Organization-type distribution (top 10)",
        """
        SELECT raw_json -> 'organizer' -> 'organization_type' ->> 'name'
                   AS org_type,
               count(*) AS n
        FROM tenders
        WHERE source_name = 'goszakup'
        GROUP BY 1
        ORDER BY n DESC
        LIMIT 10;
        """,
    ),
    (
        "5. Top 10 buyer names",
        """
        SELECT buyer_name, count(*) AS n
        FROM tenders
        WHERE source_name = 'goszakup'
        GROUP BY 1
        ORDER BY n DESC
        LIMIT 10;
        """,
    ),
]


def _format_table(headers: list[str], rows: list[tuple[object, ...]]) -> str:
    if not rows:
        return "  (no rows)"
    str_rows = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  " + "  ".join("-" * w for w in widths)
    body = [
        "  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        for row in str_rows
    ]
    return "\n".join([line, sep, *body])


async def run() -> None:
    async with async_session_factory() as session:
        for title, sql in QUERIES:
            print(f"\n{title}")
            result = await session.execute(text(sql))
            headers = list(result.keys())
            rows = [tuple(r) for r in result.all()]
            print(_format_table(headers, rows))


def main() -> int:
    _ensure_utf8_streams()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
