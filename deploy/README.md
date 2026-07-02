# Deploying tender-monitor

This directory has two systemd units and an idempotent install script.
For production, point `DATABASE_URL` at the same AWS RDS database used
by Portal. The local PostgreSQL path is kept only for small standalone
installs.

## What runs where

```
┌─────────────────┐       DB-only       ┌──────────────────────┐
│ scheduler.svc   │────────────────────▶│ AWS RDS PostgreSQL   │
└─────────────────┘                     └──────────────────────┘
┌─────────────────┐                              ▲
│   api.svc       │──────────────────────────────┘
│  uvicorn :8000  │
└─────────────────┘
         ▲
         │  HTTP over Tailscale (tailnet-only)
   browsers on the team's tailnet
```

Two long-lived systemd services owned by an unprivileged `tender` user.
The app and scheduler should write to AWS RDS so Portal can read the
same monitored tender tables.

## First boot

```bash
# 0. Pi prep (once)
sudo timedatectl set-timezone UTC
sudo apt-get update -y

# 1. Clone the repo to the canonical path
sudo mkdir -p /opt/tender-monitor
sudo chown -R "$USER" /opt/tender-monitor
git clone <your-repo-url> /opt/tender-monitor/app

# 2. Install everything against AWS RDS (venv, migrations, services)
export DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@RDS_ENDPOINT:5432/postgres?ssl=require'
sudo -E USE_LOCAL_POSTGRES=false bash /opt/tender-monitor/app/deploy/install.sh

# 3. Edit secrets the installer stubbed for you
sudo -e /opt/tender-monitor/app/.env
#   - DATABASE_URL should stay pointed at AWS RDS
#   - SMTP_HOST / SMTP_USER / SMTP_PASSWORD / SMTP_FROM  (Gmail app password)
#   - APP_BASE_URL = http://<your-tailscale-hostname>:8000

# 4. Restart so the new .env is picked up
sudo systemctl restart tender-monitor-api tender-monitor-scheduler

# 5. Tailscale (one-time)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
#   - copy the auth URL it prints, sign in
#   - note the tailnet hostname it assigns (e.g. tender-pi.tail1234.ts.net)

# 6. Smoke check from another tailnet device
#    http://<tailnet-hostname>:8000/        → tender list
#    http://<tailnet-hostname>:8000/docs    → OpenAPI explorer
```

That's it. Tailscale handles encryption + auth at the network layer, so
the API stays on plain HTTP behind it. **Do not** open port 8000 on
your router.

## Day-2 operations

### View logs

```bash
sudo journalctl -u tender-monitor-scheduler -f     # follow scheduler
sudo journalctl -u tender-monitor-api -f           # follow API
sudo journalctl -u tender-monitor-scheduler --since "1 hour ago" | less
```

structlog emits one JSON-ish event per line; useful filters:

```bash
sudo journalctl -u tender-monitor-scheduler | grep "scheduler.ingest.complete"
sudo journalctl -u tender-monitor-scheduler | grep "notifications.email"
sudo journalctl -u tender-monitor-scheduler | grep "scheduler.ingest.failed"
```

### Update the app

```bash
cd /opt/tender-monitor/app
sudo -u tender git pull
sudo USE_LOCAL_POSTGRES=false bash deploy/install.sh
# idempotent: re-pip-installs + re-migrates + restarts, preserving AWS RDS
```

### Restart / stop

```bash
sudo systemctl restart tender-monitor-api
sudo systemctl stop tender-monitor-scheduler          # gives running ingests up to 15m to drain
sudo systemctl status tender-monitor-scheduler
```

### Database backups

The DB on USB-SSD is durable, but you still want offsite copies. A
nightly `pg_dump` to the SSD plus periodic rsync off-Pi works fine:

```bash
sudo -u postgres pg_dump -Fc tender_monitor > \
  /mnt/ssd/backups/tender_monitor.$(date +%Y%m%d).dump
```

Wire it into cron and add a `find /mnt/ssd/backups -mtime +30 -delete` to
prune old files.

### Editing keywords

```bash
sudo -u tender vi /opt/tender-monitor/app/config/keywords.yaml
sudo -u tender /opt/tender-monitor/venv/bin/tender-monitor validate-keywords
sudo systemctl restart tender-monitor-scheduler
```

No DB migration needed — the matcher reloads from YAML and the next
ingest tick re-classifies existing tenders. (Re-matches don't re-send
emails, by design.)

### Adding / removing notification recipients

Open `http://<tailnet-hostname>:8000/settings/recipients` in a browser.
The settings page persists to the `email_recipients` table; the
dispatcher picks up changes on the next matched-tender event.

### Resetting all tenders (start fresh)

```bash
sudo systemctl stop tender-monitor-scheduler
sudo -u postgres psql -d tender_monitor <<'SQL'
  TRUNCATE notification_logs, feedback, tenders RESTART IDENTITY CASCADE;
  UPDATE sources SET last_run_at = NULL, last_success_at = NULL,
    consecutive_failures = 0, last_error = NULL, total_tenders_seen = 0;
SQL
sudo systemctl start tender-monitor-scheduler
```

## Things that will go wrong eventually

- **Gmail app password rotation.** After ~6 months Google sometimes
  rotates / invalidates app passwords. Symptom: every email lands as a
  `notifications.email.failed / SMTPAuthenticationError` in the journal.
  Fix: generate a fresh app password and edit `.env`.

- **Postgres on SD card if you forgot to move it.** `iostat -x 5` will
  show 100 %util on the SD card before the symptoms become obvious.
  Move `/var/lib/postgresql/15/main` to the SSD, `chown -R postgres:postgres`,
  edit `data_directory` in `postgresql.conf`, restart Postgres.

- **`samruk_kazyna` connector OOMs Chromium.** Pi 4 with only 2 GB
  can't reliably run headless Chromium under load. Symptom: that
  source repeatedly fails with browser-init errors. Disable just that
  source:
  ```sql
  UPDATE sources SET enabled = false WHERE name = 'samruk_kazyna';
  ```
  and `systemctl restart tender-monitor-scheduler`. Re-enable when you
  have a beefier Pi or move that one connector to a separate host.

- **Disk fills up because of journal accumulation.** Cap it:
  ```bash
  sudo journalctl --vacuum-size=500M
  ```
  or set `SystemMaxUse=500M` in `/etc/systemd/journald.conf`.

- **Tailscale forgets its key on reboot.** Use `tailscale up --auth-key
  tskey-...` once with a reusable key from the admin console, or
  configure Tailscale's systemd integration (`sudo tailscale set
  --auto-update`).

## What's intentionally NOT included

- **No nginx / Caddy / TLS** — Tailscale provides encrypted transport
  end-to-end. Adding a reverse proxy here would be dead weight.
- **No public DNS** — same reason; use the tailnet hostname.
- **No auth on the FastAPI app** — the Pi is the perimeter; the
  tailnet is the auth boundary. If you ever expose this publicly, the
  app needs real auth first.
- **No Docker** — at this scale (one Pi, two services, one DB)
  Docker is more operational burden than help. Plain systemd ages
  better.
