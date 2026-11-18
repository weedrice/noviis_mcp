from __future__ import annotations

import json
import logging
import logging.config
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, quote_plus
from uuid import uuid4

from config import LOG_DIR, LOG_JSON, LOG_LEVEL


@dataclass
class RequestLogContext:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    private_values: set[str] = field(default_factory=set)


# SDK child tasks inherit the same request-local object; newly discovered search
# text stays redacted through tool error logging and request completion.
request_log_context: ContextVar[RequestLogContext | None] = ContextVar("request_log_context", default=None)


def _prepare_context(record: logging.LogRecord) -> None:
    context = request_log_context.get()
    if context is not None:
        record.request_id = context.request_id


def _sanitize_log(value: Any) -> Any:
    context = request_log_context.get()
    return sanitize_error_data(value, known_secrets=tuple(context.private_values) if context else ())


def _log_extra(key: str, value: Any) -> Any:
    # A short query (e.g. "a") must not corrupt the generated correlation ID.
    if key == "request_id" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    if key == "outcome" and value in ("success", "error", "invalid_input", "http_error", "transport_error",
                                      "contract_error", "timeout", "cancelled", "disconnected", "incomplete", "overloaded"):
        return value
    if key == "http_method" and value in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return value
    return _sanitize_log(_sanitize_secret_key(key, value))


LOG_EVENT_NAMES = frozenset({
        "noviis_api_request", "noviis_api_response", "noviis_api_complete", "noviis_api_unauthorized",
        "noviis_api_forbidden", "noviis_api_server_error", "noviis_api_server_error_retry",
        "noviis_api_unhandled_status", "mcp_request_complete", "httpx_request", "httpcore_event",
})


def _log_message(record: logging.LogRecord) -> str:
    message = record.getMessage()
    # Fixed event names are not copies of a coincidentally equal search.
    return message if message in LOG_EVENT_NAMES else _sanitize_log(message)


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


def safe_log_params(params: Any) -> dict[str, Any]:
    """Only bounded pagination and a known content filter belong in diagnostics."""
    if not params:
        return {}
    result = {key: params[key] for key in ("page", "size", "limit")
              if key in params and type(params[key]) is int and 0 <= params[key] <= 1_000_000}
    if params.get("contentType") in ("ALL", "POST", "COMMENT"):
        result["contentType"] = params["contentType"]
    return result


def _prepare_http_record(record: logging.LogRecord) -> None:
    """HTTPX URLs and httpcore wire diagnostics can contain arbitrary user data.

    Keep safe method/status facts from the pinned HTTPX request event. Application
    events provide paths and failures; wire headers and reason phrases are omitted.
    Also called by formatters so privacy does not depend on handler configuration.
    """
    if getattr(record, "_http_privacy_prepared", False):
        return
    if record.name in {"httpx", "httpx2"} or record.name.startswith(("httpx.", "httpx2.")):
        args = record.args
        if record.msg == 'HTTP Request: %s %s "%s %d %s"' and isinstance(args, tuple) and len(args) == 5:
            if args[0] in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                record.http_method = args[0]
            if type(args[3]) is int and 100 <= args[3] <= 599:
                record.status_code = args[3]
        record.msg = "httpx_request"
    elif record.name in {"httpcore", "httpcore2"} or record.name.startswith(("httpcore.", "httpcore2.")):
        record.msg = "httpcore_event"
    else:
        return
    record.args = ()
    record.exc_info = record.exc_text = record.stack_info = None
    record._http_privacy_prepared = True


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _prepare_context(record)
        _prepare_http_record(record)
        # Render first: redacting a format string can remove its % placeholders.
        message = record.getMessage()
        if isinstance(record.args, dict):
            for key, value in record.args.items():
                if _is_secret_key(key) and str(value):
                    message = message.replace(str(value), "****")
        record.msg = message if message in LOG_EVENT_NAMES else _sanitize_log(message)
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in LOG_RECORD_BASE_FIELDS:
                continue
            record.__dict__[key] = _log_extra(key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        _prepare_context(record)
        _prepare_http_record(record)
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _log_message(record),
        }
        if record.exc_info:
            payload["exception"] = _sanitize_log(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = _sanitize_log(record.exc_text)
        if record.stack_info:
            payload["stack"] = _sanitize_log(record.stack_info)

        for key, value in _extra_record_items(record):
            payload[key] = _log_extra(key, value)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        return _sanitize_log(super().formatException(exc_info))

    def formatStack(self, stack_info) -> str:
        return _sanitize_log(super().formatStack(stack_info))

    def format(self, record: logging.LogRecord) -> str:
        _prepare_context(record)
        _prepare_http_record(record)
        record.msg = _log_message(record)
        record.args = ()
        if record.exc_text:
            record.exc_text = _sanitize_log(record.exc_text)
        base = super().format(record)
        extras = {key: _log_extra(key, value) for key, value in _extra_record_items(record)}
        if extras:
            return f"{_sanitize(base)} {json.dumps(extras, ensure_ascii=False)}"
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
