#!/usr/bin/env bash
#
# Tender Monitor — Raspberry Pi installer.
#
# Idempotent: re-running is safe. Each step skips if already done.
# Assumes:
#   - Raspberry Pi OS Bookworm (or any Debian 12-ish) with systemd
#   - This repo is already cloned at /opt/tender-monitor/app
#     (or pass APP_DIR=/where/it/lives as an env var)
#   - You ran this with sudo
#
# What it does:
#   1. apt-installs python3.11, venv, pip, postgresql
#   2. Creates the `tender` system user
#   3. Creates the postgres role + DB
#   4. Sets up the venv + pip-installs the project
#   5. Stub-creates .env from .env.example if missing
#   6. Runs alembic migrations and seeds sources
#   7. Installs + starts the two systemd units
#
# Re-run any time to pick up code changes (it will reinstall the package +
# re-run migrations, then restart the services).

set -euo pipefail

APP_USER="${APP_USER:-tender}"
APP_DIR="${APP_DIR:-/opt/tender-monitor/app}"
VENV_DIR="${VENV_DIR:-/opt/tender-monitor/venv}"
DB_NAME="${DB_NAME:-tender_monitor}"
DB_USER="${DB_USER:-tender}"

if [[ $EUID -ne 0 ]]; then
  echo "error: run with sudo" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "error: APP_DIR ($APP_DIR) does not exist. Clone the repo there first:" >&2
  echo "  sudo mkdir -p /opt/tender-monitor && sudo chown -R \$USER /opt/tender-monitor" >&2
  echo "  git clone <repo> $APP_DIR" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ----------------------------------------------------------------------
echo "[1/7] apt: python3.11, postgresql"
apt-get update -y
apt-get install -y \
  python3.11 python3.11-venv python3-pip \
  postgresql postgresql-contrib \
  ca-certificates curl git

# ----------------------------------------------------------------------
echo "[2/7] create user: $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$(dirname "$APP_DIR")"

# ----------------------------------------------------------------------
echo "[3/7] postgres: role + database"

# Persist the generated password in a root-owned cache file so re-runs
# always know what it is — the original bug was a brace-group scope
# issue where DB_PASSWORD could be empty by the time the .env patcher
# tried to use it, leaving DATABASE_URL with the .env.example
# placeholder. Caching to disk makes both the re-run and the .env
# patch fully deterministic.
DB_PW_CACHE="${DB_PW_CACHE:-/etc/tender-monitor.dbpw}"
if [[ -z "${DB_PASSWORD:-}" && -s "$DB_PW_CACHE" ]]; then
  DB_PASSWORD="$(cat "$DB_PW_CACHE")"
fi
if [[ -z "${DB_PASSWORD:-}" ]]; then
  DB_PASSWORD="$(openssl rand -hex 16)"
  echo "  generated db password (cached at $DB_PW_CACHE): $DB_PASSWORD"
fi
umask 077
printf '%s' "$DB_PASSWORD" > "$DB_PW_CACHE"
chmod 600 "$DB_PW_CACHE"

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  # Role exists — make sure its password matches the cache. Idempotent.
  sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD'" >/dev/null
else
  sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD'"
fi

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

# pgcrypto is required for gen_random_uuid() server defaults.
sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null

# Detect the actual listening port. On Debian, if 5432 is already taken
# (eg. by Docker), initdb silently picks 5433. Hard-coding 5432 in the
# DATABASE_URL leaves us unable to connect even when everything else is
# correct — exactly the bug we hit on Pi setup.
DB_PORT="$(sudo -u postgres psql -tAc 'SHOW port' | tr -d '[:space:]')"
if [[ -z "$DB_PORT" ]]; then
  DB_PORT=5432
fi
echo "  postgres listening on port $DB_PORT"

# ----------------------------------------------------------------------
echo "[4/7] venv + pip install"
if [[ ! -d "$VENV_DIR" ]]; then
  sudo -u "$APP_USER" python3.11 -m venv "$VENV_DIR"
fi
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -e "$APP_DIR"

# ----------------------------------------------------------------------
echo "[5/7] .env (chmod 600, owned by $APP_USER)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "  wrote $APP_DIR/.env from .env.example"
fi

# Always force DATABASE_URL to the canonical value built from the
# cached DB password. Safe to re-run; the line is overwritten in
# place. This means a stale .env left over from a previous install
# always gets healed.
NEW_DB_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${DB_NAME}?ssl=disable"
if grep -q "^DATABASE_URL=" "$APP_DIR/.env"; then
  # POSIX sed escape on the URL: ``,`` is a rare separator that won't
  # appear in any of these substrings.
  sed -i "s,^DATABASE_URL=.*,DATABASE_URL=${NEW_DB_URL}," "$APP_DIR/.env"
else
  printf '\nDATABASE_URL=%s\n' "$NEW_DB_URL" >> "$APP_DIR/.env"
fi
echo "  DATABASE_URL set to postgresql+asyncpg://${DB_USER}:<cached-password>@127.0.0.1:${DB_PORT}/${DB_NAME}?ssl=disable"

# Warn if SMTP fields are still blank — the notifier silently no-ops
# without them and you'd find out only when nothing arrives.
if grep -q '^SMTP_HOST=\s*$' "$APP_DIR/.env" || grep -q '^SMTP_PASSWORD=\s*$' "$APP_DIR/.env"; then
  echo "  ⚠ SMTP_HOST or SMTP_PASSWORD is blank in $APP_DIR/.env"
  echo "    The web UI and scheduler will run fine; the notifier will fail to send."
fi

chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# ----------------------------------------------------------------------
echo "[6/7] migrations + seed sources"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/alembic' upgrade head"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/tender-monitor' seed-sources"

# ----------------------------------------------------------------------
echo "[7/7] systemd units"
install -m 644 "$SCRIPT_DIR/systemd/tender-monitor-api.service" /etc/systemd/system/
install -m 644 "$SCRIPT_DIR/systemd/tender-monitor-scheduler.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tender-monitor-api.service
systemctl enable --now tender-monitor-scheduler.service

echo
echo "Done. Status:"
systemctl --no-pager status tender-monitor-api.service | head -3
systemctl --no-pager status tender-monitor-scheduler.service | head -3
echo
echo "Next:"
echo "  - Edit $APP_DIR/.env (SMTP_*, APP_BASE_URL=http://\$TAILSCALE_HOST:8000)"
echo "  - sudo systemctl restart tender-monitor-api tender-monitor-scheduler"
echo "  - View logs: journalctl -u tender-monitor-scheduler -f"
echo "  - Tailscale: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
