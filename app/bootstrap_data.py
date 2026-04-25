"""
Bootstrap Data Script.

One-time bulk import of 1-year OHLCV data for all stocks.
Handles 5,000+ stocks by chunking requests into 30-minute windows
and supporting resume-on-restart via a checkpoint file.

Usage:
    # Full bootstrap (all exchanges)
    python bootstrap_data.py

    # Filter to NSE_EQ only — recommended, cuts API calls by ~50%
    python bootstrap_data.py --exchange NSE_EQ

    # Resume an interrupted run from where it left off
    python bootstrap_data.py --resume

    # Combine: NSE only + resume
    python bootstrap_data.py --exchange NSE_EQ --resume

    # Dry run — just count stocks, no API calls
    python bootstrap_data.py --dry-run
"""

import argparse
import os
import sys
import time
from datetime import date

import psycopg2.extras

import app.config as config
from app.db import get_connection
from app.data_ingestion import (
    UpstoxClient,
    OHLCVWriter,
    ActiveStockUpdater,
    AuthError,
    fetch_and_store_chunked,
)
from app.indicator_engine import IndicatorEngine
from app.logging_setup import setup_logging

logger = setup_logging("bootstrap_data")


def load_instrument_keys(exchange_filter: str | None) -> list[str]:
    """
    Load instrument keys from ticker_symbol.

    Args:
        exchange_filter: If set (e.g. ``"NSE_EQ"``), only return keys whose
                         instrument_key starts with that prefix.
                         Pass None to load all keys.

    Returns:
        List of instrument_key strings.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if exchange_filter:
                cur.execute(
                    "SELECT instrument_key FROM classification.ticker_symbol "
                    "WHERE instrument_key LIKE %s ORDER BY id ASC;",
                    (f"{exchange_filter}|%",),
                )
            else:
                cur.execute(
                    "SELECT instrument_key FROM classification.ticker_symbol ORDER BY id ASC;"
                )
            return [row["instrument_key"] for row in cur.fetchall()]
    finally:
        conn.close()


def main(
    dry_run: bool = False,
    exchange_filter: str | None = None,
    resume: bool = True,
) -> int:
    start = time.time()
    logger.info(
        "Bootstrap job started. dry_run=%s, exchange_filter=%s, resume=%s",
        dry_run, exchange_filter or "ALL", resume,
    )

    # ------------------------------------------------------------------ #
    # 1. Load instrument keys
    # ------------------------------------------------------------------ #
    try:
        keys = load_instrument_keys(exchange_filter)
    except Exception as exc:
        logger.error("Failed to load instrument keys: %s", exc, exc_info=True)
        return 1

    logger.info(
        "Loaded %d instrument keys%s.",
        len(keys),
        f" (exchange={exchange_filter})" if exchange_filter else "",
    )

    if not keys:
        logger.warning("No instrument keys found. Check classification.ticker_symbol table.")
        return 1

    if dry_run:
        # Estimate runtime
        batch_size = config.UPSTOX_WINDOW_MAX_REQUESTS
        n_batches = (len(keys) + batch_size - 1) // batch_size
        est_minutes = n_batches * 30
        logger.info(
            "Dry run — %d stocks, %d batch(es) of %d, "
            "estimated total time: ~%d minutes.",
            len(keys), n_batches, batch_size, est_minutes,
        )
        return 0

    # ------------------------------------------------------------------ #
    # 2. Fetch Upstox token
    # ------------------------------------------------------------------ #
    token = os.getenv(config.UPSTOX_ACCESS_TOKEN_ENV)
    if not token:
        logger.error(
            "Environment variable %s is not set.", config.UPSTOX_ACCESS_TOKEN_ENV
        )
        return 1

    client = UpstoxClient(token)
    writer = OHLCVWriter()
    to_date = date.today().isoformat()

    # ------------------------------------------------------------------ #
    # 3. Chunked OHLCV fetch (window-aware, checkpoint-resumable)
    # ------------------------------------------------------------------ #
    logger.info(
        "Fetching 1-year OHLCV for %d stocks up to %s "
        "(batch size: %d, window: %ds).",
        len(keys), to_date,
        config.UPSTOX_WINDOW_MAX_REQUESTS,
        config.UPSTOX_WINDOW_SECONDS,
    )

    try:
        updated, failed = fetch_and_store_chunked(
            client, writer, keys, to_date, resume=resume
        )
    except AuthError as exc:
        logger.error("Authentication error: %s", exc)
        logger.error(
            "Run was checkpointed. Fix your token and re-run with --resume."
        )
        return 1

    logger.info("OHLCV fetch done: %d updated, %d failed.", updated, failed)

    # ------------------------------------------------------------------ #
    # 4. Update is_screener_active flags
    # ------------------------------------------------------------------ #
    updater = ActiveStockUpdater()
    activated, deactivated = updater.update_active_flags()
    logger.info("Active flags: %d activated, %d deactivated.", activated, deactivated)

    # ------------------------------------------------------------------ #
    # 5. Compute indicators for all active stocks
    # ------------------------------------------------------------------ #
    engine = IndicatorEngine()
    processed, errors = engine.run_all_active()
    logger.info("Indicators: %d processed, %d errors.", processed, errors)

    elapsed = time.time() - start
    logger.info(
        "Bootstrap job completed in %.0fm %.0fs.",
        elapsed // 60, elapsed % 60,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap 1-year OHLCV data for all stocks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bootstrap_data.py --exchange NSE_EQ          # NSE only (recommended)
  python bootstrap_data.py --resume                   # resume interrupted run
  python bootstrap_data.py --exchange NSE_EQ --resume # both
  python bootstrap_data.py --dry-run                  # estimate only
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count stocks and estimate runtime without making API calls.",
    )
    parser.add_argument(
        "--exchange",
        metavar="PREFIX",
        default=None,
        help=(
            "Filter stocks by instrument_key prefix, e.g. NSE_EQ. "
            "Recommended — cuts API calls by ~50%% by skipping BSE duplicates."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore any existing checkpoint and start from the beginning.",
    )
    args = parser.parse_args()
    sys.exit(
        main(
            dry_run=args.dry_run,
            exchange_filter=args.exchange,
            resume=not args.no_resume,
        )
    )
