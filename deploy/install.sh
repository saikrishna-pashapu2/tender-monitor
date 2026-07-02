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
PYTHON_BIN="${PYTHON_BIN:-python3}"
DB_NAME="${DB_NAME:-tender_monitor}"
DB_USER="${DB_USER:-tender}"
EXTERNAL_DATABASE_URL="${DATABASE_URL:-}"
if [[ -z "$EXTERNAL_DATABASE_URL" && -f "$APP_DIR/.env" ]]; then
  EXTERNAL_DATABASE_URL="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -n 1 | cut -d= -f2- || true)"
fi
EXTERNAL_DATABASE_URL="${EXTERNAL_DATABASE_URL%$'\r'}"
EXTERNAL_DATABASE_URL="${EXTERNAL_DATABASE_URL%$'\n'}"
USE_LOCAL_POSTGRES="${USE_LOCAL_POSTGRES:-}"
if [[ -z "$USE_LOCAL_POSTGRES" ]]; then
  if [[ -n "$EXTERNAL_DATABASE_URL" && "$EXTERNAL_DATABASE_URL" != *"127.0.0.1"* && "$EXTERNAL_DATABASE_URL" != *"localhost"* ]]; then
    USE_LOCAL_POSTGRES=false
  else
    USE_LOCAL_POSTGRES=true
  fi
fi

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
if [[ "$USE_LOCAL_POSTGRES" == "true" ]]; then
  echo "[1/7] apt: python3, postgresql"
else
  echo "[1/7] apt: python3"
fi
apt-get update -y
APT_PACKAGES=(python3 python3-venv python3-pip ca-certificates curl git)
if [[ "$USE_LOCAL_POSTGRES" == "true" ]]; then
  APT_PACKAGES+=(postgresql postgresql-contrib)
fi
apt-get install -y "${APT_PACKAGES[@]}"

# ----------------------------------------------------------------------
echo "[2/7] create user: $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$(dirname "$APP_DIR")"

# ----------------------------------------------------------------------
echo "[2b/7] certs: ETS-Tender scoped CA bundle"
if [[ -f /usr/local/share/ca-certificates/sectigo-public-server-authentication-ca-dv-r36.crt ]]; then
  rm -f /usr/local/share/ca-certificates/sectigo-public-server-authentication-ca-dv-r36.crt
  update-ca-certificates >/dev/null || true
fi
install -d -o "$APP_USER" -g "$APP_USER" /opt/tender-monitor/certs
ETS_CERT_DIR=/opt/tender-monitor/certs "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

CERT_DIR = Path(os.environ["ETS_CERT_DIR"])
BUNDLE = CERT_DIR / "ets-tender-ca-bundle.pem"

DV_R36_URL = "http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt"
DV_R36_FP = "8C:54:C3:34:B6:6B:A4:E4:26:77:2A:F4:A3:F9:13:6C:19:A1:AE:C7:29:FD:B2:8C:53:5C:07:A5:A4:EF:22:E0"

ROOT_R46_URL = "http://crt.sectigo.com/SectigoPublicServerAuthenticationRootR46.p7c"
ROOT_R46_FP = "7B:B6:47:A6:2A:EE:AC:88:BF:25:7A:A5:22:D0:1F:FE:A3:95:E0:AB:45:C7:3F:93:F6:56:54:EC:38:F2:5A:06"


def _download(url: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False)
    handle.close()
    urllib.request.urlretrieve(url, handle.name)
    return Path(handle.name)


def _fingerprint(path: Path) -> str:
    output = subprocess.check_output(
        ["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"],
        text=True,
    )
    return output.strip().split("=", 1)[1]


def _pem_from_der(url: str, expected_fingerprint: str) -> str:
    der_path = _download(url)
    pem_handle = tempfile.NamedTemporaryFile(delete=False)
    pem_handle.close()
    pem_path = Path(pem_handle.name)
    try:
        subprocess.check_call(
            ["openssl", "x509", "-inform", "DER", "-in", str(der_path), "-out", str(pem_path)]
        )
        actual = _fingerprint(pem_path)
        if actual != expected_fingerprint:
            raise SystemExit(f"unexpected certificate fingerprint: {actual}")
        return pem_path.read_text()
    finally:
        der_path.unlink(missing_ok=True)
        pem_path.unlink(missing_ok=True)


def _root_from_pkcs7(url: str, expected_fingerprint: str) -> str:
    p7_path = _download(url)
    try:
        pem_text = subprocess.check_output(
            ["openssl", "pkcs7", "-inform", "DER", "-in", str(p7_path), "-print_certs"],
            text=True,
        )
    finally:
        p7_path.unlink(missing_ok=True)

    blocks: list[str] = []
    current: list[str] = []
    inside = False
    for line in pem_text.splitlines():
        if "BEGIN CERTIFICATE" in line:
            inside = True
            current = [line]
        elif "END CERTIFICATE" in line and inside:
            current.append(line)
            blocks.append("\n".join(current) + "\n")
            inside = False
        elif inside:
            current.append(line)

    for block in blocks:
        cert_handle = tempfile.NamedTemporaryFile("w", delete=False)
        cert_handle.write(block)
        cert_handle.close()
        cert_path = Path(cert_handle.name)
        try:
            if _fingerprint(cert_path) == expected_fingerprint:
                subject = subprocess.check_output(
                    ["openssl", "x509", "-in", str(cert_path), "-noout", "-subject"],
                    text=True,
                ).strip()
                issuer = subprocess.check_output(
                    ["openssl", "x509", "-in", str(cert_path), "-noout", "-issuer"],
                    text=True,
                ).strip()
                if subject.removeprefix("subject=") != issuer.removeprefix("issuer="):
                    raise SystemExit("expected Sectigo Root R46 to be self-signed")
                return block
        finally:
            cert_path.unlink(missing_ok=True)
    raise SystemExit("expected Sectigo Root R46 certificate not found")


BUNDLE.write_text(
    _pem_from_der(DV_R36_URL, DV_R36_FP)
    + "\n"
    + _root_from_pkcs7(ROOT_R46_URL, ROOT_R46_FP)
)
print(f"  wrote {BUNDLE}")
PY
chown "$APP_USER:$APP_USER" /opt/tender-monitor/certs/ets-tender-ca-bundle.pem
chmod 0644 /opt/tender-monitor/certs/ets-tender-ca-bundle.pem

# ----------------------------------------------------------------------
if [[ "$USE_LOCAL_POSTGRES" == "true" ]]; then
  echo "[3/7] postgres: local role + database"

  # Persist the generated password in a root-owned cache file so re-runs
  # always know what it is. Caching to disk makes both the re-run and the
  # .env patch fully deterministic.
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
    # Role exists; make sure its password matches the cache. Idempotent.
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD'" >/dev/null
  else
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD'"
  fi

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
    sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

  # pgcrypto is required for gen_random_uuid() server defaults.
  sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null

  # Detect the actual listening port. On Debian, if 5432 is already taken
  # (eg. by Docker), initdb silently picks 5433.
  DB_PORT="$(sudo -u postgres psql -tAc 'SHOW port' | tr -d '[:space:]')"
  if [[ -z "$DB_PORT" ]]; then
    DB_PORT=5432
  fi
  echo "  postgres listening on port $DB_PORT"
else
  echo "[3/7] postgres: external database"
  echo "  USE_LOCAL_POSTGRES=false; local PostgreSQL setup skipped"
fi

# ----------------------------------------------------------------------
echo "[4/7] venv + pip install"
if [[ ! -d "$VENV_DIR" ]]; then
  sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -e "$APP_DIR"
if sudo -u "$APP_USER" "$VENV_DIR/bin/python" -c "import playwright" >/dev/null 2>&1; then
  install -d -o "$APP_USER" -g "$APP_USER" /opt/tender-monitor/ms-playwright
  "$VENV_DIR/bin/python" -m playwright install-deps chromium
  sudo -u "$APP_USER" env PLAYWRIGHT_BROWSERS_PATH=/opt/tender-monitor/ms-playwright \
    "$VENV_DIR/bin/python" -m playwright install chromium
fi

# ----------------------------------------------------------------------
echo "[5/7] .env (chmod 600, owned by $APP_USER)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "  wrote $APP_DIR/.env from .env.example"
fi

if [[ "$USE_LOCAL_POSTGRES" == "true" ]]; then
  # Always force DATABASE_URL to the canonical local value built from
  # the cached DB password. Safe to re-run; the line is overwritten.
  NEW_DB_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${DB_NAME}?ssl=disable"
  DB_LOG_URL="postgresql+asyncpg://${DB_USER}:<cached-password>@127.0.0.1:${DB_PORT}/${DB_NAME}?ssl=disable"
else
  if [[ -z "$EXTERNAL_DATABASE_URL" ]]; then
    EXTERNAL_DATABASE_URL="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -n 1 | cut -d= -f2- || true)"
  fi
  if [[ -z "$EXTERNAL_DATABASE_URL" || "$EXTERNAL_DATABASE_URL" == *"127.0.0.1"* || "$EXTERNAL_DATABASE_URL" == *"localhost"* ]]; then
    echo "error: USE_LOCAL_POSTGRES=false requires DATABASE_URL to point at AWS RDS" >&2
    exit 1
  fi
  NEW_DB_URL="$EXTERNAL_DATABASE_URL"
  DB_LOG_URL="$(EXTERNAL_DATABASE_URL="$EXTERNAL_DATABASE_URL" "$PYTHON_BIN" - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

url = os.environ["EXTERNAL_DATABASE_URL"]
parts = urlsplit(url)
host = parts.hostname or "<host>"
port = f":{parts.port}" if parts.port else ""
path = parts.path or ""
print(urlunsplit((parts.scheme, f"<redacted>@{host}{port}", path, "", "")))
PY
)"
fi

NEW_DB_URL="$NEW_DB_URL" "$PYTHON_BIN" - "$APP_DIR/.env" <<'PY'
import os
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
new_line = f"DATABASE_URL={os.environ['NEW_DB_URL']}"
lines = env_path.read_text(encoding="utf-8").splitlines()
for index, line in enumerate(lines):
    if line.startswith("DATABASE_URL="):
        lines[index] = new_line
        break
else:
    lines.append(new_line)
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
echo "  DATABASE_URL set to $DB_LOG_URL"

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
if [[ "$USE_LOCAL_POSTGRES" == "true" ]]; then
  sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/alembic' upgrade head"
else
  echo "  external DB mode: skipping Alembic; Portal monitored schema is managed separately"
fi
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
