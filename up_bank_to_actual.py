#!/usr/bin/env python3
"""
Up Bank → Actual Budget Sync
Polls Up Bank every 3 hours and imports transactions into Actual Budget.

Dependencies:
    pip install requests actualpy python-dotenv

Usage:
    python up_bank_to_actual.py                  # continuous polling
    python up_bank_to_actual.py --once           # single sync then exit
    python up_bank_to_actual.py --list-accounts  # print Up + Actual account
                                                 # names/IDs then exit

Setup order:
    1. Copy .env.example to .env
    2. Add UP_BANK_TOKEN, ACTUAL_PASSWORD, ACTUAL_SERVER_URL, ACTUAL_SYNC_ID
    3. Run --list-accounts to see your account IDs
    4. Add ACCOUNT_n_UP_ID / ACCOUNT_n_ACTUAL pairs to .env
    5. Run --once to test, then start the continuous service

Account mapping (.env format):
    ACCOUNT_1_UP_ID=314971b7-xxxx
    ACCOUNT_1_ACTUAL=Up Transaction
    ACCOUNT_2_UP_ID=f82fdec9-xxxx
    ACCOUNT_2_ACTUAL=Up Savings
    (add as many numbered pairs as needed)

Telegram setup (optional):
    1. Message @BotFather, send /newbot, copy the token.
    2. Start a chat with your bot, visit:
       https://api.telegram.org/bot<TOKEN>/getUpdates
       Send the bot a message first, then copy the "id" from "chat".
    3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

Field name reference (verified against actualpy source):
    reconcile_transaction params : date, account, payee, notes, amount,
                                   imported_id, cleared, imported_payee,
                                   already_matched
    Transactions model columns   : financial_id (stores imported_id),
                                   imported_description (stores imported_payee),
                                   cleared (INTEGER 0/1), tombstone (soft delete)
"""

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import decimal
import argparse
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from actual import Actual
from actual.queries import get_accounts, get_transactions, reconcile_transaction

# Load .env file if present — safe to call even if the file doesn't exist
load_dotenv()

# ── Telegram — read at module level since they have safe empty defaults ────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Constants ─────────────────────────────────────────────────────────────────

API_BASE        = "https://api.up.com.au/api/v1"
PAGE_SIZE       = 100
MAX_RETRIES     = 5      # retries on HTTP 429 / transient network errors
RETRY_BACKOFF   = 30     # seconds to wait between retries (doubles each attempt)
                         # sequence: 30s, 60s, 120s, 240s — total ~7.5min before giving up

def _parse_int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    """Read an optional integer env var, falling back to default if missing or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(
            f"WARNING: {name}='{raw}' is not a valid integer — using default of {default}.",
            file=sys.stderr,
        )
        return default
    if not (min_val <= value <= max_val):
        print(
            f"WARNING: {name}={value} is outside the allowed range "
            f"({min_val}–{max_val}) — using default of {default}.",
            file=sys.stderr,
        )
        return default
    return value

# POLL_INTERVAL_HOURS — how often to sync (default: 3, range: 1–24)
POLL_INTERVAL = _parse_int_env("POLL_INTERVAL_HOURS", default=3, min_val=1, max_val=24) * 60 * 60

# LOOKBACK_DAYS — how far back to fetch transactions on each poll (default: 30, range: 1–90)
LOOKBACK_DAYS = _parse_int_env("LOOKBACK_DAYS", default=30, min_val=1, max_val=90)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Graceful shutdown ─────────────────────────────────────────────────────────

# Docker sends SIGTERM before SIGKILL. We catch it so the process exits cleanly
# rather than being killed mid-sync, which could leave Actual in a dirty state.
_shutdown = False

def _handle_sigterm(signum, frame):
    global _shutdown
    log.info("SIGTERM received — will exit after current poll completes.")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_sigterm)

# ── Env helpers ───────────────────────────────────────────────────────────────

def require_env(name: str) -> str:
    """Read a required env var, print a clear error and exit if missing."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(
            f"ERROR: Required environment variable '{name}' is not set.\n"
            f"Add it to your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def load_account_map() -> dict:
    """
    Build {up_account_id: actual_account_name} from numbered env var pairs:
        ACCOUNT_1_UP_ID / ACCOUNT_1_ACTUAL
        ACCOUNT_2_UP_ID / ACCOUNT_2_ACTUAL  ...
    Stops at the first n where both vars are absent.
    """
    mapping = {}
    n = 1
    while True:
        up_id       = os.environ.get(f"ACCOUNT_{n}_UP_ID", "").strip()
        actual_name = os.environ.get(f"ACCOUNT_{n}_ACTUAL", "").strip()

        if not up_id and not actual_name:
            break

        if not up_id or not actual_name:
            log.warning(
                "ACCOUNT_%d_UP_ID and ACCOUNT_%d_ACTUAL must both be set — skipping pair %d.",
                n, n, n,
            )
            n += 1
            continue

        mapping[up_id] = actual_name
        n += 1

    return mapping

# ── Telegram notifications ────────────────────────────────────────────────────

def notify_error(message: str, exc: Exception = None):
    """Send an error notification via Telegram. Silently skips if not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"🚨 *Up→Actual Sync Error*\n`{timestamp}`\n\n{message}"

    if exc:
        tb = "".join(
            __import__("traceback").format_exception(type(exc), exc, exc.__traceback__)
        )
        max_tb = 4096 - len(text) - 20
        if max_tb > 0:
            text += f"\n\n```\n{tb[-max_tb:]}\n```"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as tg_exc:
        log.warning("Failed to send Telegram notification: %s", tg_exc)

# ── Up Bank API ───────────────────────────────────────────────────────────────

def up_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def up_ping(token: str) -> bool:
    resp = requests.get(f"{API_BASE}/util/ping", headers=up_headers(token), timeout=10)
    if resp.status_code == 200:
        log.info("✓ Authenticated with Up Bank API")
        return True
    log.error("Up Bank auth failed: %s %s", resp.status_code, resp.text)
    return False


def fetch_up_accounts(token: str) -> list:
    """Return all Up Bank accounts."""
    resp = requests.get(f"{API_BASE}/accounts", headers=up_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_up_transactions_for_account(token: str, account_id: str, since: str) -> list:
    """
    Fetch all transactions for a specific account, following pagination.

    Uses filter[since] on settledAt rather than createdAt so that transactions
    created before the lookback window but settled within it are still captured.
    Pending (HELD) transactions do not have a settledAt, so the Up API falls
    back to createdAt for those automatically.
    """
    params   = {"page[size]": PAGE_SIZE, "filter[since]": since}
    url      = f"{API_BASE}/accounts/{account_id}/transactions"
    all_txns = []

    while url:
        # Retry loop handles HTTP 429 rate limiting with exponential backoff
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url,
                    headers=up_headers(token),
                    params=params,
                    timeout=15,
                )
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if retry_after else wait
                    log.warning("Rate limited by Up Bank — retrying in %ds (attempt %d/%d)",
                                wait, attempt, MAX_RETRIES)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as req_exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    "Network error (%s) — retrying in %ds (attempt %d/%d)",
                    type(req_exc).__name__, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
        else:
            raise requests.HTTPError(f"Failed after {MAX_RETRIES} attempts")

        body = resp.json()
        all_txns.extend(body.get("data", []))
        params = {}
        url = (body.get("links") or {}).get("next")

    return all_txns

# ── List accounts command ─────────────────────────────────────────────────────

def cmd_list_accounts(token: str, password: str, server_url: str, sync_id: str):
    """Print all Up Bank and Actual Budget account names/IDs then exit."""

    print("\n── Up Bank Accounts ──────────────────────────────────────────")
    try:
        for acct in fetch_up_accounts(token):
            attrs     = acct["attributes"]
            name      = attrs.get("displayName", "Unknown")
            acct_type = attrs.get("accountType", "")
            balance   = attrs["balance"]["value"]
            currency  = attrs["balance"]["currencyCode"]
            print(f"  {name:<30}  {acct_type:<12}  {currency} {float(balance):>10.2f}  id: {acct['id']}")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print("\n── Actual Budget Accounts ────────────────────────────────────")
    try:
        with Actual(base_url=server_url, password=password, file=sync_id) as actual:
            for acct in get_accounts(actual.session):
                print(f"  {acct.name:<30}  id: {acct.id}")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    mapping = load_account_map()
    print("\n── Current Account Mapping (from .env) ───────────────────────")
    if not mapping:
        print("  (no ACCOUNT_n_UP_ID / ACCOUNT_n_ACTUAL pairs configured yet)")
    else:
        for up_id, actual_name in mapping.items():
            print(f"  Up {up_id}  →  Actual '{actual_name}'")

    print()
    sys.exit(0)

# ── Conversion helpers ────────────────────────────────────────────────────────

def up_amount_to_decimal(value_str: str) -> decimal.Decimal:
    """Convert Up Bank amount string (e.g. '-12.50') to Decimal for actualpy."""
    return decimal.Decimal(value_str)


def format_transaction(txn: dict, account_name: str) -> str:
    """Human-readable one-liner for logging."""
    attrs       = txn["attributes"]
    description = attrs.get("description", "Unknown")
    amount      = attrs["amount"]["value"]
    currency    = attrs["amount"]["currencyCode"]
    created_at  = attrs.get("createdAt", "")
    status      = attrs.get("status", "")
    return (
        f"{created_at[:19]}  {status:<9}  "
        f"{currency} {float(amount):>10.2f}  "
        f"{description}  [{account_name}]"
    )

# ── Actual Budget helpers ─────────────────────────────────────────────────────

def build_actual_account_lookup(actual) -> dict:
    """
    Fetch all Actual accounts once and return {name_lower: account_object}.
    Called once per sync rather than once per account to avoid repeated
    database queries inside the account loop.
    """
    return {acct.name.lower(): acct for acct in get_accounts(actual.session)}


def sync_to_actual(
    up_txns_by_account: dict,
    account_map: dict,
    actual,
    since_date: str,
):
    """
    Reconcile Up Bank transactions into Actual and delete any stale pending
    transactions that have disappeared from Up (failed/dropped transactions).
    """
    since_dt       = datetime.fromisoformat(since_date.replace("Z", "+00:00")).date()
    today          = datetime.now(timezone.utc).date()
    total_changed  = 0
    total_deleted  = 0

    # Fetch all Actual accounts once rather than once per Up account
    actual_accounts = build_actual_account_lookup(actual)

    for up_acct_id, txns in up_txns_by_account.items():
        account_name = account_map[up_acct_id]
        acct         = actual_accounts.get(account_name.lower())

        if acct is None:
            msg = f"No Actual account named '{account_name}' — skipping."
            log.warning(msg)
            notify_error(msg)
            continue

        up_ids = {t["id"] for t in txns}

        # reconcile_transaction params verified against actualpy source:
        #   imported_id    → stored as financial_id column in the database
        #   imported_payee → stored as imported_description column
        #   already_matched prevents false duplicate matching for identical
        #   (date, amount) pairs within the same batch
        already_matched = []
        for txn in txns:
            attrs   = txn["attributes"]
            # Use settledAt date when available so the transaction date in
            # Actual reflects when the payment actually cleared, falling back
            # to createdAt for pending (HELD) transactions.
            settled_at  = attrs.get("settledAt")
            created_at  = attrs.get("createdAt", "")
            date_str    = settled_at[:10] if settled_at else created_at[:10]
            date        = datetime.strptime(date_str, "%Y-%m-%d").date()
            amount      = up_amount_to_decimal(attrs["amount"]["value"])
            payee       = attrs.get("description", "Unknown")
            notes       = attrs.get("message", "") or ""
            cleared     = attrs.get("status") == "SETTLED"

            t = reconcile_transaction(
                actual.session,
                date=date,
                account=acct,
                payee=payee,
                notes=notes,
                amount=amount,
                imported_id=txn["id"],
                cleared=cleared,
                imported_payee=attrs.get("rawText") or payee,
                already_matched=already_matched,
            )
            already_matched.append(t)
            if t.changed():
                total_changed += 1
                log.info("  [%s] %s  %s  %.2f", account_name, date, payee, float(amount))

        # ── Run rules on newly imported transactions ───────────────────────
        # Runs your Actual payee/category rules against the transactions
        # imported in this batch. Passing already_matched limits it to only
        # the transactions we just touched rather than the entire database.
        if already_matched:
            actual.run_rules(already_matched)
            log.info("  [%s] Rules applied to %d transaction(s)", account_name, len(already_matched))

        # cleared is an INTEGER column (0/1), so we compare explicitly.
        # t.delete() sets tombstone=1 (soft delete), consistent with actualpy.
        actual_txns = get_transactions(actual.session, since_dt, today, account=acct)
        stale = [
            t for t in actual_txns
            if t.cleared == 0 and t.financial_id and t.financial_id not in up_ids
        ]
        for t in stale:
            t.delete()
            log.info("  Deleted stale pending: %s  %s", t.financial_id, t.notes)
        total_deleted += len(stale)

    actual.commit()
    log.info("✓ Sync complete — changed: %d  stale deleted: %d", total_changed, total_deleted)

# ── Poll cycle ────────────────────────────────────────────────────────────────

def poll(token: str, password: str, server_url: str, sync_id: str, account_map: dict):
    since = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    log.info("Fetching transactions since %s (%d-day window)", since, LOOKBACK_DAYS)

    # Fetch transactions per account, tracking successes and failures separately
    up_txns_by_account: dict = {}
    fetch_errors: list       = []

    for up_acct_id, account_name in account_map.items():
        try:
            txns = fetch_up_transactions_for_account(token, up_acct_id, since)
            up_txns_by_account[up_acct_id] = txns
            log.info("  '%s': fetched %d transaction(s)", account_name, len(txns))
            for txn in txns:
                log.info("    %s", format_transaction(txn, account_name))
        except requests.HTTPError as exc:
            msg = f"HTTP error fetching '{account_name}': {exc}"
            log.error(msg)
            notify_error(msg, exc)
            fetch_errors.append(account_name)
        except requests.RequestException as exc:
            msg = f"Network error fetching '{account_name}': {exc}"
            log.error(msg)
            notify_error(msg, exc)
            fetch_errors.append(account_name)

    # Only skip the Actual sync if we have no successful fetches at all.
    # Partial failures (some accounts errored) still sync whatever we have.
    if not up_txns_by_account:
        log.info("No transactions fetched — skipping Actual sync.")
        return

    if fetch_errors:
        log.warning(
            "Proceeding with partial data — %d account(s) failed to fetch: %s",
            len(fetch_errors), ", ".join(fetch_errors),
        )

    try:
        with Actual(base_url=server_url, password=password, file=sync_id) as actual:
            sync_to_actual(up_txns_by_account, account_map, actual, since)

    except Exception as exc:
        msg = f"Actual Budget error: {exc}"
        log.error(msg, exc_info=True)
        notify_error(msg, exc)

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Up Bank transactions to Actual Budget.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python up_bank_to_actual.py                  # continuous polling\n"
            "  python up_bank_to_actual.py --once           # single sync then exit\n"
            "  python up_bank_to_actual.py --list-accounts  # show all account IDs/names"
        ),
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single sync then exit.")
    parser.add_argument("--list-accounts", action="store_true",
                        help="Print all Up Bank and Actual account names/IDs then exit.")
    args = parser.parse_args()

    # Credentials are read here — not at module level — so the script can
    # be imported or partially run (e.g. --list-accounts) without all vars set.
    token      = require_env("UP_BANK_TOKEN")
    password   = require_env("ACTUAL_PASSWORD")
    server_url = require_env("ACTUAL_SERVER_URL")
    sync_id    = require_env("ACTUAL_SYNC_ID")

    # --list-accounts only needs the four connection vars above, not the mapping
    if args.list_accounts:
        cmd_list_accounts(token, password, server_url, sync_id)
        return

    account_map = load_account_map()
    if not account_map:
        print(
            "ERROR: No account mapping configured.\n"
            "Set ACCOUNT_1_UP_ID + ACCOUNT_1_ACTUAL (and so on) in your .env.\n"
            "Run --list-accounts to see available account IDs.",
            file=sys.stderr,
        )
        sys.exit(1)

    log.info("Loaded %d account mapping(s):", len(account_map))
    for up_id, actual_name in account_map.items():
        log.info("  %s  →  '%s'", up_id, actual_name)

    if not up_ping(token):
        msg = "Could not authenticate with Up Bank — check your token."
        log.error(msg)
        notify_error(msg)
        raise SystemExit(msg)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        log.info("✓ Telegram notifications enabled")
    else:
        log.info("  Telegram not configured — errors will only appear in logs")

    if args.once:
        log.info("── One-off sync starting at %s ──",
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
        poll(token, password, server_url, sync_id, account_map)
        log.info("── One-off sync complete ──")
        sys.exit(0)

    log.info("Polling every %d hour(s), %d-day lookback. Press Ctrl+C to stop.",
             POLL_INTERVAL // 3600, LOOKBACK_DAYS)

    while not _shutdown:
        log.info("── Poll starting at %s ──",
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
        poll(token, password, server_url, sync_id, account_map)

        if _shutdown:
            break

        log.info("Next poll in %d hour(s).\n", POLL_INTERVAL // 3600)

        # Sleep in short intervals so SIGTERM is handled promptly
        # rather than waiting out the full poll interval
        deadline = time.monotonic() + POLL_INTERVAL
        while time.monotonic() < deadline and not _shutdown:
            time.sleep(1)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user.")