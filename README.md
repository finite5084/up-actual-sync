# up-actual-sync

Automatically syncs [Up Bank](https://up.com.au) transactions into [Actual Budget](https://actualbudget.org) every few hours. Runs as a Docker container on your home server.

- Fetches transactions from Up Bank via their API
- Imports into Actual Budget using [actualpy](https://github.com/bvanelli/actualpy)
- Handles pending → cleared updates automatically
- Removes pending transactions that never settled (failed/reversed payments)
- Sends error notifications via Telegram (optional)
- Fully configurable via environment variables — no code changes needed

---

## Requirements

- Docker and Docker Compose
- A running [Actual Budget](https://actualbudget.org) instance
- An [Up Bank personal access token](https://api.up.com.au)

---

## Quick Start

**1. Clone the repo**

```bash
git clone https://github.com/your-username/up-actual-sync
cd up-actual-sync
```

**2. Create your `.env` file**

```bash
cp .env.example .env
```

**3. Fill in the required values**

```bash
nano .env
```

At minimum you need:

```
UP_BANK_TOKEN=up:yeah:your_token_here
ACTUAL_PASSWORD=your_actual_password
ACTUAL_SERVER_URL=http://actual-budget:5006
ACTUAL_SYNC_ID=your-sync-id
```

**4. Update the Docker network name**

Edit `docker-compose.yml` and set the `name:` under `networks` to match the Docker network your Actual Budget container is on:

```bash
docker inspect <actual-container-name> | grep -A5 Networks
```

**5. Build and start**

```bash
docker compose up -d
docker compose logs -f
```

---

## Finding Your Account IDs

Before configuring the account mapping you need to know your Up Bank account IDs. With the container running:

```bash
docker exec up-actual-sync python up_bank_to_actual.py --list-accounts
```

This prints all your Up Bank accounts with their IDs, and all your Actual Budget accounts with their names, plus the current mapping. Copy the IDs into your `.env`.

---

## Configuration

All configuration is done via environment variables in your `.env` file.

### Required

| Variable | Description |
|---|---|
| `UP_BANK_TOKEN` | Up Bank personal access token from [api.up.com.au](https://api.up.com.au) |
| `ACTUAL_PASSWORD` | Your Actual Budget server password |
| `ACTUAL_SERVER_URL` | URL of your Actual Budget server e.g. `http://actual-budget:5006` |
| `ACTUAL_SYNC_ID` | The sync ID of your budget — found in Actual under Settings → Show advanced settings |

### Account Mapping

Map each Up Bank account to an Actual Budget account using numbered pairs. Add as many as you need.

```
ACCOUNT_1_UP_ID=314971b7-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ACCOUNT_1_ACTUAL=Up Transaction

ACCOUNT_2_UP_ID=f82fdec9-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ACCOUNT_2_ACTUAL=Up Savings
```

### Optional

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL_HOURS` | `3` | How often to sync in hours (1–24) |
| `LOOKBACK_DAYS` | `30` | How many days back to fetch on each poll (1–90) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token for error notifications |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for error notifications |
| `TZ` | `UTC` | Timezone for log timestamps e.g. `Australia/Sydney` |

---

## Commands

All commands run against the already-running container:

```bash
# List all Up Bank and Actual account names/IDs
docker exec up-actual-sync python up_bank_to_actual.py --list-accounts

# Run a single sync immediately
docker exec up-actual-sync python up_bank_to_actual.py --once

# Watch live logs
docker compose logs -f
```

---

## Telegram Notifications

To receive error alerts on your phone:

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot` — copy the token it gives you
2. Start a chat with your new bot, then open this URL in a browser (after sending the bot at least one message):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. Find the `"id"` field inside `"chat"` — that's your chat ID
4. Add both to your `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   TELEGRAM_CHAT_ID=987654321
   ```

---

## How It Works

On each poll the script:

1. Fetches transactions from the last `LOOKBACK_DAYS` days for each mapped account via the Up Bank API
2. Connects to your Actual Budget instance and reconciles each transaction using `reconcile_transaction` — this handles deduplication by `imported_id`, updates pending → cleared status, and runs your Actual payee rules automatically
3. Checks for any pending transactions in Actual that have disappeared from Up (failed/reversed payments) and soft-deletes them
4. Sleeps until the next poll

No state is written to disk — everything is held in memory and Actual Budget handles deduplication natively via the `imported_id` field.

---

## Security Notes

- The Up Bank personal access token is **read-only** — it cannot initiate payments or modify your account
- Secrets are stored only in `.env` which is excluded from git via `.gitignore`
- The container runs as a non-root user (`appuser`)
- No data is written to disk inside the container

---

## Troubleshooting

**`Name or service not known` when connecting to Actual**

The container can't reach your Actual Budget server. Make sure:
- The container is on the same Docker network as Actual Budget
- `ACTUAL_SERVER_URL` uses the container name, not an IP address e.g. `http://actual-budget:5006`

**`No Actual account named '...' — skipping`**

The name in `ACCOUNT_n_ACTUAL` doesn't match any account in Actual Budget. Run `--list-accounts` and check for typos — the match is case-insensitive but must otherwise be exact.

**Transactions not appearing in Actual**

Check the logs with `docker compose logs -f`. If transactions are being fetched but not imported, it's likely an account name mismatch. If nothing is being fetched, check your `UP_BANK_TOKEN` is valid and `LOOKBACK_DAYS` is large enough.

**Pending transactions not being removed**

Only pending transactions within the `LOOKBACK_DAYS` window are checked. If a transaction was created more than `LOOKBACK_DAYS` ago and never settled, increase `LOOKBACK_DAYS` temporarily and run `--once`.

---

## Dependencies

- [actualpy](https://github.com/bvanelli/actualpy) — Python client for Actual Budget
- [requests](https://pypi.org/project/requests/) — HTTP client for the Up Bank API
- [python-dotenv](https://pypi.org/project/python-dotenv/) — `.env` file loading

---

## Disclaimer
This project was developed with the assistance of Claude by Anthropic. All code has been reviewed and tested, but use at your own risk. This project is not affiliated with Up Bank or Actual Budget.
