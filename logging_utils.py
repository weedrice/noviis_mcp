from __future__ import annotations

import json
import logging
import logging.config
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, quote_plus

from config import LOG_DIR, LOG_JSON, LOG_LEVEL


TOKEN_PATTERN = re.compile(r"noviis_agt_[A-Za-z0-9_-]+")
BEARER_PATTERN = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)
SECRET_FIELD_PATTERN = re.compile(
    r"""(?P<prefix>["']?(?:x-noviis-internal-secret|authorization|agent_token|token|api_key|secret|ssh_key|password)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)""",
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
SDK_VALIDATION_MESSAGES = {
    "Failed to validate request:": "Failed to validate request: input omitted.",
    "Failed to validate notification:": "Failed to validate notification: input omitted.",
    "Message that failed validation:": "Message that failed validation: input omitted.",
}


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render first: redacting a format string can remove its % placeholders.
        message = record.getMessage()
        # MCP SDK 1.26 logs method-specific validation errors and raw messages
        # before Tool.run. These root-logger diagnostics may contain arbitrary
        # unlabeled secrets, so token-pattern masking is insufficient. Keep the
        # classification and severity, but never retain their input diagnostics.
        # Actual HTTP tests cover this narrow SDK source/message adapter.
        if (record.pathname.replace("\\", "/").lower().endswith("/mcp/shared/session.py")
                and record.funcName == "_receive_loop"):
            for prefix, safe_message in SDK_VALIDATION_MESSAGES.items():
                if message.startswith(prefix):
                    message = safe_message
                    record.exc_info = None
                    record.exc_text = None
                    record.stack_info = None
                    break
        if isinstance(record.args, dict):
            for key, value in record.args.items():
                if _is_secret_key(key) and str(value):
                    message = message.replace(str(value), "****")
        record.msg = _sanitize(message)
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in LOG_RECORD_BASE_FIELDS:
                continue
            record.__dict__[key] = _sanitize_secret_key(key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = _sanitize(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = _sanitize(record.exc_text)
        if record.stack_info:
            payload["stack"] = _sanitize(record.stack_info)

        for key, value in _extra_record_items(record):
            payload[key] = _sanitize_secret_key(key, value)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {key: _sanitize_secret_key(key, value) for key, value in _extra_record_items(record)}
        if extras:
            return _sanitize(f"{base} {json.dumps(extras, ensure_ascii=False)}")
        return _sanitize(base)


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



def sanitize_error_data(value: Any, *, known_secrets: tuple[str | None, ...] = ()) -> Any:
    """Redact backend error diagnostics, without touching successful payloads.

    Known per-request credentials also cover unlabeled echoes and common escaped
    forms. Keep this context local: no identity is retained between MCP requests.
    """
    variants = set()
    for secret in known_secrets:
        if secret:
            escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
            variants.update((secret, quote(secret, safe=""), quote_plus(secret, safe=""),
                             escaped, escaped.replace("/", "\\/")))
    # Percent escapes are case-insensitive, but credential letters are not.
    patterns = [re.sub(r"%[0-9A-Fa-f]{2}", lambda match: "(?i:" + match[0] + ")", re.escape(item))
                for item in sorted(variants, key=len, reverse=True)]
    pattern = re.compile("|".join(patterns)) if patterns else None

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return _sanitize(pattern.sub("****", item) if pattern is not None else item)
        if isinstance(item, dict):
            return {clean(key): "****" if _is_secret_key(key) else clean(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            cleaned = [clean(nested) for nested in item]
            return tuple(cleaned) if isinstance(item, tuple) else cleaned
        return item

    return clean(value)


def _sanitize_secret_key(key: Any, value: Any) -> Any:
    if _is_secret_key(key):
        return "****"
    return _sanitize(value)


def _is_secret_key(key: Any) -> bool:
    return any(token in str(key).lower() for token in ("authorization", "token", "secret", "password", "api_key", "ssh_key"))
