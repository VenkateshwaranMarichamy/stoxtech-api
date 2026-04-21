"""Central configuration for the stk_fund screener."""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "stk_fund"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# ---------------------------------------------------------------------------
# Upstox API
# ---------------------------------------------------------------------------
UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_ACCESS_TOKEN_ENV = "UPSTOX_ACCESS_TOKEN"

# Rate limiting
UPSTOX_REQUEST_DELAY_SECONDS = 0.5   # 2 req/s — conservative, well under 50/s limit
UPSTOX_RETRY_DELAY_SECONDS = 1.0
UPSTOX_MAX_RETRIES = 3

# Chunked bootstrap — Upstox hard limit: 2,000 requests per 30-minute window
UPSTOX_WINDOW_MAX_REQUESTS = 1800    # stay under 2000 with a safety margin
UPSTOX_WINDOW_SECONDS = 30 * 60     # 30-minute rolling window
UPSTOX_INTER_BATCH_PAUSE_SECONDS = 5 # extra pause between batches

# Checkpoint file — stores last successfully processed instrument_key index
BOOTSTRAP_CHECKPOINT_FILE = "./logs/bootstrap_checkpoint.json"

# ---------------------------------------------------------------------------
# Screener thresholds
# ---------------------------------------------------------------------------
MIN_AVG_DAILY_TRADED_VALUE = 10_000_000   # ₹1 crore
ACTIVE_STOCK_LOOKBACK_DAYS = 30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = "./logs"
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
LOG_BACKUP_COUNT = 5

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_DEFAULT_PAGE_SIZE = 50
API_MAX_PAGE_SIZE = 500
API_DEFAULT_HISTORY_DAYS = 30
