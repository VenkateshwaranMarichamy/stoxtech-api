"""Shared logging configuration."""

import logging
import os
from logging.handlers import RotatingFileHandler

from config import LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(name: str) -> logging.Logger:
    """
    Return a logger that writes to stdout and a rotating log file.

    Args:
        name: Logger name (also used as the log file base name).

    Returns:
        Configured :class:`logging.Logger`.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured — avoid duplicate handlers
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT)

    # stdout
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # rotating file
    log_path = os.path.join(LOG_DIR, f"{name}.log")
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
