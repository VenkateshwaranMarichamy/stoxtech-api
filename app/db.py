"""Shared database helpers and connection management."""

import logging
import psycopg2
import psycopg2.extras
from app.config import DB_CONFIG

logger = logging.getLogger(__name__)


def get_connection():
    """Return a new psycopg2 connection using DB_CONFIG."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as exc:
        logger.error("Database connection failed: %s", exc)
        raise



