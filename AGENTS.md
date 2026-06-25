# AGENTS.md — tender-monitor

Canonical project doc. Read this before making changes.

## Project overview

`tender-monitor` watches public procurement tender platforms in Kazakhstan
(10 sources) and Uzbekistan (5 sources) — 15 sources total. Every 30–60
minutes the system pulls new tenders from each source, filters them against
ESG and credit-rating keyword groups, stores everything in Postgres, and
pushes matches to a Telegram group and an email distribution list. A FastAPI
service exposes the data to the existing internal portal, which already has
a "Tenders" tab.

Most sources expose internal JSON APIs we call directly. A few will need
HTML scraping. Each source gets its own connector implementation; connectors
are added one at a time in dedicated prompts.

## Architecture

Three independent processes run on a single VPS and share one Postgres
database:

- **scheduler** — runs all connectors on a recurring interval, normalizes
  tenders, writes them to the DB, and runs keyword matching.
- **notifier** — picks up matched tenders that haven't been sent yet and
  fans them out to Telegram and email.
- **api** — FastAPI service that serves filtered tender data to the
  internal portal's "Tenders" tab.

Processes talk to each other only through the database. There is no
in-process queue, no Redis, no message broker.

## Data flow

```
source → connector → normalize → DB → matcher → notifier
                                   ↘ api → portal
```

1. **source** — a tender platform (e.g. goszakup.gov.kz).
2. **connector** — source-specific code that fetches raw payloads.
3. **normalize** — connector maps raw payload to our canonical Tender shape.
4. **DB** — Postgres is the system of record; dedup happens here.
5. **matcher** — applies keyword groups from `config/keywords.yaml`,
   produces match rows.
6. **notifier** — reads unsent matches and dispatches Telegram + email.
7. **api** — read-only endpoints for the portal.

## Project conventions

- **SQLAlchemy 2.0 async only.** Use `DeclarativeBase`, `Mapped`,
  `mapped_column`, and `AsyncSession`. No legacy 1.4 patterns.
- **Pydantic v2 throughout.** Use `BaseModel`, `Field`, and
  `pydantic-settings` for configuration. No v1 syntax.
- **All I/O is async.** HTTP calls, DB calls, SMTP, Telegram — everything
  awaits. Sync code is reserved for pure logic (matching, normalization
  helpers).
- **Type hints everywhere.** `mypy` runs in strict mode against `src/`.
- **Structured logging via `structlog`.** Never use `print()`. JSON output
  in production, pretty console in development. Log level comes from
  settings.
- **Settings access only through the singleton.**
  `from tender_monitor.core.config import settings`. Do not read
  `os.environ` outside `core/config.py`.
- **No business logic in `__init__.py` files.** They exist to mark
  packages and (when needed) re-export a small public surface.

## Where things live

- `src/tender_monitor/core/` — config, database engine/session, logging
  setup. Imported by everything else.
- `src/tender_monitor/connectors/` — one module per tender source plus a
  shared base class. The Connector contract: see
  `src/tender_monitor/connectors/base.py` once Prompt 3 is implemented.
- `src/tender_monitor/matching/` — keyword-group matching against stored
  tenders.
- `src/tender_monitor/notifications/` — Telegram and email senders plus
  templates.
- `src/tender_monitor/scheduler/` — APScheduler wiring that drives
  connector runs and the matcher.
- `src/tender_monitor/api/` — FastAPI app, routes, and dependencies.
- `src/tender_monitor/cli.py` — `click` entrypoint for `run-scheduler`,
  `run-notifier`, and `run-api`.
- `config/keywords.yaml` — keyword groups consumed by the matcher.
- `config/sources.yaml` — per-source configuration consumed by connectors.
- `alembic/` — schema migrations. Models live under `src/` and are
  imported by `alembic/env.py`.
- `tests/` — pytest suite with shared fixtures in `conftest.py` and
  sample payloads in `tests/fixtures/`.
- `scripts/` — one-off operational scripts (backfills, manual reruns).

## Data model

Four tables, all in Postgres:

- **`sources`** — one row per tender platform we monitor. Tracks display
  metadata, scraping cadence, and per-source health counters
  (`last_run_at`, `last_success_at`, `consecutive_failures`,
  `total_tenders_seen`). Keyed by short `name` (e.g. `goszakup`).
- **`tenders`** — the canonical record for every tender we've ever seen,
  one row per `(source_name, external_id)`. That pair is the system's
  uniqueness invariant; the matcher and notifier rely on it.
- **`feedback`** — operator feedback on individual tenders
  (`good_match`, `bad_match`, `missed`). Free-text `created_by` because
  there is no auth; the portal supplies the identifier.
- **`notification_logs`** — append-only log of every Telegram/email send
  attempt, keyed to a tender and including the channel-specific
  `external_message_id` so later edits-on-update are possible.

Two invariants worth remembering:

1. **`raw_json` is always populated.** Connectors store the unmodified
   payload they received from the source, even when normalization
   succeeds. It is the single source of truth for everything we saw at
   ingest time and is GIN-indexed for ad-hoc queries.
2. **Match results, AI fields, and `external_message_id` are nullable
   and filled in by later pipeline stages**, not on insert. A tender
   row exists as soon as the connector sees it; the matcher fills
   `matched_groups` / `match_details`, the LLM stage fills
   `ai_relevance_score` / `ai_summary` / `ai_processed_at`, and the
   notifier writes `notification_logs` rows after dispatch.

`canonical_id` is a self-FK on `tenders` that supports cross-source dedup
(e.g. the same tender appearing on `goszakup` and `samruk-kazyna`). It is
nullable; the dedup algorithm is not part of the initial pipeline and
sets the field later.

## Data philosophy

`raw_json` is the source of truth for every tender; the normalized columns
are projections of it that exist purely so the matcher, API, and indexes
can work efficiently. There is one `tenders` table for all 15 sources, not
one table per source — this is what keeps the matcher, notifier, API, and
cross-source dedup straightforward instead of fanning out into 16 parallel
implementations. New fields that appear in a source's API don't trigger a
migration: they land in `raw_json` automatically, and we project them into
a typed column later only if a query or notification template actually
needs them. Per-source tables would only be justified if sources
represented fundamentally different kinds of objects (tenders vs. live
auctions vs. RFIs); all 15 sources here are tenders, so a single shared
table is the right shape.

## Connector contract

Every source has exactly one `Connector` subclass living in
`src/tender_monitor/connectors/`, named after the source. Each concrete
connector declares a class-level `source_name`, implements
`_fetch_raw(since)` (returns the raw items the source gave us, may raise
`FetchError`) and `_normalize(raw)` (turns one raw item into a
`TenderUpsert`, may raise `ParseError`), and registers itself at module
import via the `@register` decorator from
`tender_monitor.connectors.registry`.

The base class owns orchestration: `fetch_latest` records timing,
captures the raw item count, calls `_fetch_raw`, then walks the items
and runs `_normalize` on each one. Concrete connectors never override
`fetch_latest` — that is what keeps every source observable in the same
shape (the scheduler logs the same fields, the API exposes the same
counters, ops dashboards work for all sources without per-source
plumbing).

The error contract is split deliberately: per-item normalization
failures are caught, logged at WARNING, and recorded in
`FetchResult.partial_errors` so a single rotten payload never sinks the
whole run. Whole-fetch failures — network errors, non-2xx status codes,
top-level response parsing — raise `FetchError` from `_fetch_raw` and
propagate uncaught so the scheduler can mark the source as failed and
back off.

Tests for each connector use saved JSON (or HTML) fixtures under
`tests/fixtures/<source_name>/` and never make live HTTP calls; HTTP
behavior is exercised through `httpx.MockTransport` so CI is
deterministic and offline.

Adding a new source is a fixed checklist with no framework changes:
write `src/tender_monitor/connectors/<source_name>.py`, drop fixtures
into `tests/fixtures/<source_name>/`, add `tests/connectors/test_<source_name>.py`,
and register the source in `config/sources.yaml`.

## Keyword matching

Keyword groups live in `config/keywords.yaml` and are tuned by the team
directly — adding, removing, or rewording an entry never requires a
schema migration or a code change. Each top-level group (`credit_rating`,
`esg`, plus whatever else gets added later) is a triple of `phrases`,
`tokens`, and `exclude_if_contains`.

Phrases match as case-insensitive substrings; tokens match as
case-insensitive whole words (regex `\b<token>\b`, Unicode-aware so
Cyrillic word boundaries Just Work). Tokens with whitespace are
explicitly rejected by the loader — multi-word proper nouns like
"Эксперт РА" belong in `phrases`, where substring semantics are right.
`exclude_if_contains` short-circuits: if any of those substrings appears
in the text, the whole group is skipped before phrase or token checks
run, which is how we keep "кредитная карта" out of the `credit_rating`
bucket. Each group is evaluated independently, so a tender can match
zero, one, or several groups; the matcher returns both `matched_groups`
and a `match_details` dict shaped to drop straight into the JSONB
`tenders.match_details` column.

`match_tender(tender, config)` builds the haystack from the tender's
title plus `buyer_name` plus a best-effort walk over any lots inside
`raw_json["_lots"]`, picking up string values whose key ends in `_ru`
or is exactly `name`/`description`. That contract is generic per source
— other sources can shape their `raw_json` payload similarly and inherit
the same matcher with no per-source code path. If a future source needs
something more elaborate, add a per-source extractor; until then, one
walk function fits all sources.

Morphological lemmatization (pymorphy3) is intentionally NOT used in
v1. We list inflected variants explicitly in YAML
(`кредитный/кредитного/кредитному/кредитным рейтинг…`) and will only
upgrade to lemmatization if false negatives become a real problem in
production. The cost-of-explicit-list is paid by the team once per
phrase; the cost of lemmatization is paid by every match call forever
plus a meaningful complexity tax on debugging "why did this not match?"

Match results are populated on the tender row by the scheduler at
insert time (Prompt 6 wires this) — this module's responsibility is
purely the function `match_tender(tender, config) -> MatchResult`.

Two CLI commands exist for tuning: `tender-monitor match-text "<text>"`
prints which groups fired and which phrases/tokens triggered them, and
`tender-monitor validate-keywords` reports group/phrase/token counts and
surfaces a parse or validation error on a malformed YAML.

## Scheduler and ingestion

The scheduler is layered into three modules in
`src/tender_monitor/scheduler/`. `upsert.py` is the pure DB-write
boundary: take a `TenderUpsert` + `MatchResult`, produce an
`UpsertResult`, no orchestration. `ingest.py` is the per-source
orchestrator: load source, compute the `since` cursor, run the
connector, run the matcher, call `upsert_tender` for each row, and
update source health — one `ingest_source(name)` call equals one run.
`runner.py` is APScheduler plumbing: one `IntervalTrigger` job per
enabled source, plus signal-driven graceful shutdown. The CLI wires
`run-once <source>` directly to `ingest_source` and `run-scheduler`
to a `Runner` instance.

The `since` window is the previous run's `last_run_at`, or
`now - 7 days` on the first ever run. We record `last_run_at` (and
`last_success_at`, on success) as the time the run *started*, not when
it finished — the next run treats that timestamp as the lower bound,
and recording the END would silently skip every tender published
during the previous run's duration.

Change tracking is deliberately narrow. `TRACKED_FIELDS` in `upsert.py`
is `(title, status, deadline_at, value_amount)`; updates to those
emit a `scheduler.tender.changed` INFO log and append a JSONB entry to
`tenders.change_log` shaped as `{"at": iso, "fields": {field:
{"old": ..., "new": ...}}}`. Other field updates apply silently. The
matcher's results (`matched_groups`, `match_details`) are overwritten
on every run rather than tracked — keyword YAML changes should take
effect on the next ingest without producing a change-log entry per
tender. `raw_json` is also overwritten silently for the same reason
(deep-diffing a 50 KB payload per tender would be all noise); the
TRACKED_FIELDS whitelist is the v1 surface area for "this tender
changed" alerts that Prompt 7's notifier will read.

Source health lives on the `sources` table: `last_run_at` is set every
time an ingest starts, `last_success_at` is set only on success,
`consecutive_failures` is incremented on every failure and reset to 0
on success, and `last_error` carries the most recent
`TypeName: message`. Ops will read these via Prompt 8's health
endpoint.

Failure isolation is split between two layers. A connector that throws
`FetchError` (or any unexpected exception) inside `ingest_source`
gets recorded on the Source row in a *separate* session — the main
ingest session has been rolled back, but the failure metadata is
durable — and then re-raised. The scheduler's job wrapper in
`runner.py` catches the re-raise at the boundary so one bad source
never kills the others. Inside the per-tender loop, a buggy matcher
is caught defensively: we log `scheduler.matcher_failed` and treat
the row as no-match, so a bad keyword regex can never cost us a
tender row.

The scheduler passes a `known_external_ids` hint to each connector
before `fetch_latest`. It's the set of `external_id` values for that
source whose `last_seen_at` falls within the past
`KNOWN_IDS_LOOKBACK_DAYS` (14 days). The base class stashes the hint
on `self._known_external_ids` for the duration of `_fetch_raw` and
clears it in a `finally`. Connectors are free to ignore the hint;
today only `national_bank` consults it, to skip per-lot detail
fetches for tenders we've already processed (the source spends ~96%
of its per-cycle requests re-fetching dates we already have, and the
hint cuts that to near-zero on the steady-state run). A follow-up
will add `observed_external_ids` to `FetchResult` so the scheduler
can advance `last_seen_at` on listing-seen-but-detail-skipped rows
without re-running the upsert pipeline; without that, skipped
tenders' `last_seen_at` drifts gradually behind reality.

## How to run locally

```bash
cp .env.example .env
# fill in DATABASE_URL and the secrets you need locally

alembic upgrade head

python -m tender_monitor.cli run-scheduler
python -m tender_monitor.cli run-notifier
python -m tender_monitor.cli run-api
```

Run `ruff check src tests`, `mypy src`, and `pytest` before committing.

## Web UI

The first user-facing surface is a read-only browsing UI served by the
FastAPI app in `src/tender_monitor/api/`. Stack is intentionally boring
— FastAPI + Jinja2 templates + HTMX 1.9.10 + Tailwind via the Play CDN
+ Lucide icons. There is no Node, no build step, and no client
framework; every page is server-rendered and HTMX swaps a single
`#results` partial when filters or sort change. Two pages ship: a list
view at `/` with sidebar filters (country, source, matched group,
free-text search on title + buyer, date range, sort) and a detail view
at `/tenders/{id}` with the full key-value table, "why this matched"
breakdown, lot list pulled from `raw_json._lots`, change log, and a
collapsed raw-JSON section. All filter state lives in the URL so any
view is bookmarkable and the back button just works.

Every HTML route has a JSON twin under `/api/...` taking the same
query parameters and returning either `TenderSummary[]` (list) or the
full `TenderRead` (detail). The same query helpers in
`api/queries.py` back both surfaces, so HTML and JSON cannot drift.
The JSON API is what the existing portal will integrate against;
FastAPI's `/docs` renders the OpenAPI explorer. This is read-only on
purpose: feedback buttons, source-health, AI relevance scoring, and
any write paths each get their own follow-up prompt. The UI does not
authenticate; it is meant to live behind the existing portal's auth
perimeter.

## Deployment

Production target is a Raspberry Pi (4 or 5) on USB-SSD, fronted by
Tailscale rather than a public reverse proxy. Everything operational
lives under `deploy/`: two systemd units (`tender-monitor-api.service`,
`tender-monitor-scheduler.service`) run as an unprivileged `tender`
user, an idempotent `install.sh` does the apt + postgres + venv +
migrations + systemd dance in one shot, and `deploy/README.md` is the
first-boot checklist and ops runbook (logs, updates, backups, common
failure modes). Tailscale handles transport encryption and access
control end-to-end — the API binds plain HTTP on `:8000` and is only
reachable from tailnet-joined devices, so there's no nginx, no public
DNS, and no TLS plumbing inside the app. Database backups are the
operator's responsibility: nightly `pg_dump -Fc` to the same SSD plus
periodic rsync off-box; the deploy README has the snippet. The
`samruk_kazyna` connector launches headless Chromium and may need to
be disabled (`UPDATE sources SET enabled = false`) on smaller Pis.
