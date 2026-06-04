from __future__ import annotations

import json
import logging
import logging.config
import re
from datetime import datetime, timezone
from typing import Any

from config import LOG_DIR, LOG_JSON, LOG_LEVEL


TOKEN_PATTERN = re.compile(r"noviis_agt_[A-Za-z0-9]+")
BEARER_PATTERN = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)
SECRET_FIELD_PATTERN = re.compile(
    r"(?P<prefix>\"?(?:authorization|token|agent_token|api_key|secret|ssh_key|password)\"?\s*[:=]\s*\"?)(?P<value>[^\",}\s]+)",
    re.IGNORECASE,
)
LOG_RECORD_BASE_FIELDS = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)
ROTATING_FILE_HANDLER = {
    "class": "logging.handlers.RotatingFileHandler",
    "maxBytes": 10 * 1024 * 1024,
    "backupCount": 5,
    "encoding": "utf-8",
}


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _sanitize(value) for key, value in record.args.items()}
            else:
                record.args = tuple(_sanitize(value) for value in record.args)

        for key, value in list(record.__dict__.items()):
            if key in LOG_RECORD_BASE_FIELDS:
                continue
            record.__dict__[key] = _sanitize(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in _extra_record_items(record):
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = dict(_extra_record_items(record))
        if extras:
            return f"{base} {json.dumps(extras, ensure_ascii=False)}"
        return base


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app_log_path = LOG_DIR / "app.log"
    access_log_path = LOG_DIR / "access.log"

    formatter_name = "json" if LOG_JSON else "plain"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": _filter_config(),
            "formatters": _formatter_config(),
            "handlers": {
                "console_app": {
                    "class": "logging.StreamHandler",
                    "level": LOG_LEVEL,
                    "formatter": formatter_name,
                    "filters": ["sanitize"],
                    "stream": "ext://sys.stdout",
                },
                "app_file": {
                    **ROTATING_FILE_HANDLER,
                    "level": LOG_LEVEL,
                    "formatter": formatter_name,
                    "filters": ["sanitize"],
                    "filename": str(app_log_path),
                },
                "access_file": {
                    **ROTATING_FILE_HANDLER,
                    "level": LOG_LEVEL,
                    "formatter": formatter_name,
                    "filters": ["sanitize"],
                    "filename": str(access_log_path),
                },
            },
            "loggers": {
                "": {
                    "handlers": ["console_app", "app_file"],
                    "level": LOG_LEVEL,
                },
                "uvicorn.error": {
                    "handlers": ["console_app", "app_file"],
                    "level": LOG_LEVEL,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["access_file"],
                    "level": LOG_LEVEL,
                    "propagate": False,
                },
            },
        }
    )


def build_uvicorn_log_config() -> dict[str, Any]:
    formatter_name = "json" if LOG_JSON else "plain"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": _filter_config(),
        "formatters": _formatter_config(),
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": formatter_name,
                "filters": ["sanitize"],
                "stream": "ext://sys.stdout",
            },
            "access": {
                **ROTATING_FILE_HANDLER,
                "formatter": formatter_name,
                "filters": ["sanitize"],
                "filename": str(LOG_DIR / "access.log"),
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": LOG_LEVEL, "propagate": False},
        },
    }


def _filter_config() -> dict[str, Any]:
    return {"sanitize": {"()": "logging_utils.SensitiveDataFilter"}}


def _formatter_config() -> dict[str, Any]:
    return {
        "json": {"()": "logging_utils.JsonFormatter"},
        "plain": {
            "()": "logging_utils.PlainFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    }


def _extra_record_items(record: logging.LogRecord) -> list[tuple[str, Any]]:
    return [
        (key, value)
        for key, value in record.__dict__.items()
        if not key.startswith("_") and key not in LOG_RECORD_BASE_FIELDS
    ]


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        redacted = TOKEN_PATTERN.sub("noviis_agt_****", value)
        redacted = BEARER_PATTERN.sub(r"\1****", redacted)
        redacted = SECRET_FIELD_PATTERN.sub(r"\g<prefix>****", redacted)
        return redacted
    if isinstance(value, dict):
        return {key: _sanitize_secret_key(key, nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        sanitized_items = [_sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(sanitized_items)
        if isinstance(value, set):
            return set(sanitized_items)
        return sanitized_items
    return value


def _sanitize_secret_key(key: Any, value: Any) -> Any:
    key_str = str(key).lower()
    if any(token in key_str for token in ("authorization", "token", "secret", "password", "api_key", "ssh_key")):
        return "****"
    return _sanitize(value)
