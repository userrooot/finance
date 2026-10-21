# Deploying on a Linux server (systemd)

1. Create the service user and directory:
   ```
   sudo useradd --system --create-home --shell /usr/sbin/nologin arb
   sudo mkdir -p /opt/arb && sudo chown arb:arb /opt/arb
   ```
2. Clone and install (as `arb`):
   ```
   sudo -u arb git clone <your-repo-url> /opt/arb
   cd /opt/arb
   sudo -u arb python3 -m venv .venv
   sudo -u arb .venv/bin/pip install -r requirements.txt
   ```
3. Create `/opt/arb/.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, exchange keys if needed) and restrict it:
   `sudo chmod 600 /opt/arb/.env && sudo chown arb:arb /opt/arb/.env`
4. Install and start the paper trader:
   ```
   sudo cp deploy/arb-paper.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now arb-paper
   ```
5. Logs: `journalctl -u arb-paper -f`

State is written to `data/paper/funding_carry/` (daily `state.json` backups in `backups/`, last 30 kept).

## Live trading

`arb-live.service` runs `python -m arb live --strategy funding_carry`. That command requires the Phase 4 safety
checks, `ARB_LIVE_CONFIRM` in `.env` and `live_trading: true` in `config/settings.yaml`. Do not enable it until the
paper run has been reviewed. Logs: `journalctl -u arb-live -f`.

## Docker alternative

From the repo root: `docker compose -f deploy/docker-compose.yml up -d --build` starts `paper` and `scanner`
with `data/` and `reports/` mounted and logs capped at 5 x 20 MB.

## Year-end tax export

`python -m arb tax-export --strategy funding_carry --year 2026 [--live]` writes CSVs to `reports/`.
