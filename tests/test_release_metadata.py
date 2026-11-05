from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
import logging
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from tools import version

with patch("logging_utils.configure_logging"), patch("mcp.server.fastmcp.server.configure_logging"):
    import main


class ReleaseMetadataTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)

    def test_embedded_build_identity_wins_without_repository_files(self):
        with patch.object(version, "PACKAGE_VERSION", "0.2.0"), patch.object(version, "_ROOT_DIR", Path("missing-source")):
            self.assertEqual(version.package_version(), "0.2.0")

    def test_installed_metadata_is_used_when_source_is_missing_or_unrelated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(version, "PACKAGE_VERSION", None), patch.object(version, "_ROOT_DIR", root), patch.object(version, "version", return_value="0.2.0") as installed:
                self.assertEqual(version.package_version(), "0.2.0")
                (root / "pyproject.toml").write_text('[project]\nname="other"\nversion="99"\n', encoding="utf-8")
                self.assertEqual(version.package_version(), "0.2.0")
                installed.assert_called_with("noviis-mcp")

    def test_unknown_installation_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(version, "PACKAGE_VERSION", None), patch.object(version, "_ROOT_DIR", Path(directory)), patch.object(version, "version", side_effect=PackageNotFoundError):
                self.assertEqual(version.package_version(), "0.0.0+unknown")
                self.assertEqual(version.sdk_version(), "unknown")

    def test_protocol_and_manifest_share_application_version(self):
        headers = {"Accept": "application/json, text/event-stream"}
        with TestClient(main.create_app(), base_url="http://127.0.0.1:8001") as client:
            initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "release-test", "version": "1"}}})
            self.assertEqual(initialized.status_code, 200)
            headers["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
            client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_agent_manifest", "arguments": {}}})
            data = response.json()["result"]["structuredContent"]
            self.assertEqual(initialized.json()["result"]["serverInfo"]["version"], data["package_version"])
            self.assertEqual(data["package_version"], version.package_version())
            self.assertEqual(data["sdk_version"], version.sdk_version())
            self.assertEqual(data["build_revision"], version.BUILD_REVISION)
            self.assertEqual(data["authentication_methods"], ["bearer_header", "agent_token_argument"])
