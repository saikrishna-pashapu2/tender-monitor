# tender-monitor

Internal service that monitors public procurement tender platforms in Kazakhstan
and Uzbekistan, filters tenders by ESG and credit-rating keywords, persists them
to Postgres, and pushes matches to Telegram and email. A FastAPI service exposes
the data to the internal portal.

See [CLAUDE.md](CLAUDE.md) for architecture, conventions, and project layout.

## Setup

Requires Python 3.11+ and a local Postgres instance.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .[dev]

cp .env.example .env
# fill in DATABASE_URL and any secrets you need

alembic upgrade head
```

## Running the processes

The system runs as three independent processes that share the same Postgres
database:

```bash
python -m tender_monitor.cli run-scheduler   # scrape sources + run matching
python -m tender_monitor.cli run-notifier    # send Telegram/email
python -m tender_monitor.cli run-api         # FastAPI service for the portal
```

## Development

```bash
ruff check src tests
mypy src
pytest
```
