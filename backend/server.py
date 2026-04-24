from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from .app import create_app_from_env
from .settings import get_settings


def configure_logging(log_level: str, error_log_file: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    log_path = Path(error_log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.error_log_file)
    uvicorn.run(
        create_app_from_env(),
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        log_config=None,
    )


if __name__ == "__main__":
    main()
