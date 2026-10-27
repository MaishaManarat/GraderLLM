"""Configure logging: timestamped log file under log/ plus console output."""

import logging
import os
from datetime import datetime

import config


def setup_logging() -> str:
    """Initialize root logging handlers. Returns the path to the log file."""
    os.makedirs(config.LOG_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(config.LOG_DIR, f"run_{timestamp}.log")

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(config.LOG_LEVEL)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Quieten third-party libraries that log at INFO/DEBUG by default.
    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "filelock",
        "openai",
        "requests",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_file