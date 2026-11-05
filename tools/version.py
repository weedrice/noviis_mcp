"""Application release identity, independent of the MCP SDK version."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

try:
    from tools._build_info import BUILD_REVISION, PACKAGE_VERSION
except ImportError:
    BUILD_REVISION = None
    PACKAGE_VERSION = None

_ROOT_DIR = Path(__file__).resolve().parent.parent


def package_version() -> str:
    if PACKAGE_VERSION:
        return PACKAGE_VERSION
    source = _ROOT_DIR / "pyproject.toml"
    if source.is_file():
        with source.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
        if project.get("name") == "noviis-mcp" and project.get("version"):
            return str(project["version"])
    try:
        return version("noviis-mcp")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def sdk_version() -> str:
    try:
        return version("mcp")
    except PackageNotFoundError:
        return "unknown"
