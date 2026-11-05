"""Use the same SDK argument models for tool calls and opportunity validation."""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp.tools.base import Tool
from pydantic import ValidationError

from tools.input_validation import RESOURCE_ID_PARAMS, feed_pagination, observed_comment_ids, resource_id, validate_tool_inputs
from tools.write_safety import validate_write_text


@lru_cache(maxsize=1)
def tool_contracts() -> dict[str, Tool]:
    # Import lazily: registration modules also use this validator. Building SDK
    # models does not start a server, open files, or contact the backend.
    import tools

    class Registry:
        def __init__(self):
            self.definitions = {}

        def tool(self, **kwargs):
            def register(fn):
                definition = Tool.from_function(fn, **kwargs)
                self.definitions[definition.name] = definition
                return fn
            return register

    registry = Registry()
    for name in tools.__all__:
        getattr(tools, name)(registry)
    return registry.definitions


class _ActionParams(Mapping):
    def __getitem__(self, name):
        return set(tool_contracts()[name].parameters["properties"])

    def __iter__(self):
        return iter(tool_contracts())

    def __len__(self):
        return len(tool_contracts())


OPPORTUNITY_ACTION_PARAMS = _ActionParams()


def validate_action(tool_name: str, supplied: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], str | None]:
    params = dict(supplied)
    contract = tool_contracts().get(tool_name)
    if contract is None:
        return params, sorted(params), [], f"Unsupported MCP tool in opportunity action: {tool_name}"
    # Use the same SDK compatibility step as Tool.run (JSON-encoded arrays).
    # Strict pagination still rejects numeric strings, floats and booleans.
    params = contract.fn_metadata.pre_parse_json(params)
    invalid = set(params) - set(contract.parameters["properties"])
    missing = set(contract.parameters.get("required", [])) - set(params)
    try:
        contract.fn_metadata.arg_model.model_validate(params)
    except ValidationError as exc:
        for error in exc.errors():
            if error["type"] != "missing" and error["loc"]:
                invalid.add(str(error["loc"][0]))
    for name, value in params.items():
        if name in invalid:
            continue
        try:
            validate_tool_inputs({name: value})
            if name in RESOURCE_ID_PARAMS and value is not None:
                params[name] = resource_id(value)
            if name == "observed_comment_ids":
                params[name] = observed_comment_ids(value)
            if tool_name in {"create_post", "create_comment", "create_reply", "send_note"} and name in {"title", "content"}:
                validate_write_text(name, value)
        except (ValueError, TypeError, AttributeError):
            invalid.add(name)
    if tool_name == "create_post" and params.get("board_id") is None and not params.get("board_url"):
        missing.add("board_url")  # Supplying board_id also satisfies this alternative.
    if tool_name == "create_post":
        from tools.activity import _normalize_image_file_id

        image_id = None
        if "image_file_id" not in invalid:
            try:
                image_id = _normalize_image_file_id(params.get("image_file_id"))
                if "image_file_id" in params:
                    params["image_file_id"] = image_id
            except (ValueError, TypeError, AttributeError):
                invalid.add("image_file_id")
        alt = params.get("image_alt")
        if isinstance(alt, str):
            try:
                validate_write_text("image_alt", alt)
                if len(alt) > 300:
                    raise ValueError("Image description is too long")
            except ValueError:
                invalid.add("image_alt")
            if alt.strip() and image_id is None:
                missing.add("image_file_id")
    if tool_name == "get_feed":
        try:
            feed_pagination(**{name: params.get(name) for name in ("limit", "cursor", "page", "size")})
        except (ValueError, TypeError):
            if params.get("cursor") is not None:
                invalid.add("cursor")
            if params.get("limit") is not None and params.get("size") is not None and params["limit"] != params["size"]:
                invalid.update({"limit", "size"})
    if tool_name in {"create_post", "create_comment", "create_reply", "send_note"}:
        if params.get("challenge_id") is not None and params.get("answer") is None:
            missing.add("answer")
        if params.get("answer") is not None and params.get("challenge_id") is None:
            missing.add("challenge_id")
    warning = f"Invalid or unsupported params for {tool_name}: {', '.join(sorted(invalid))}" if invalid else None
    return params, sorted(invalid), sorted(missing), warning
