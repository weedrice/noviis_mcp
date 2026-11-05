from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.deploy_release import smoke_check
from deploy.release_artifact import ReleaseIdentity, read_release_identity, write_manifest


class Response(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = headers or {}


class RuntimeReadbackTest(unittest.TestCase):
    identity = ReleaseIdentity("0.2.0", "a" * 40)

    def run_probe(self, *, manifest=None, initialized_version="0.2.0", tool_error=False,
                  rpc_error=False, text_only=False, session=False):
        calls = []
        if manifest is None:
            manifest = {"package_version": "0.2.0", "build_revision": "a" * 40}

        def request(request, **kwargs):
            if request.get_method() == "DELETE":
                calls.append({"method": "DELETE"})
                return Response({})
            body = json.loads(request.data)
            calls.append(body)
            method = body["method"]
            if method == "initialize":
                return Response({"result": {"protocolVersion": "2025-03-26",
                                 "serverInfo": {"version": initialized_version}}},
                                {"Mcp-Session-Id": "synthetic-session"} if session else {})
            if method == "notifications/initialized":
                return Response({})
            if method == "tools/list":
                return Response({"result": {"tools": [{"name": name} for name in
                                ("get_agent_home", "create_post", "send_note", "get_agent_manifest")]}})
            self.assertEqual(body["params"], {"name": "get_agent_manifest", "arguments": {}})
            if rpc_error:
                return Response({"error": {"code": -32603, "message": "synthetic failure"}})
            result = {"content": [{"type": "text", "text": json.dumps(manifest)}]}
            if not text_only:
                result["structuredContent"] = manifest
            if tool_error:
                result["isError"] = True
            return Response({"result": result})

        self.calls = calls
        with patch("deploy.deploy_release.urllib.request.urlopen", side_effect=request):
            smoke_check("http://127.0.0.1/synthetic", expected_identity=self.identity)
        return calls

    def test_running_manifest_identity_is_read_back_in_both_mcp_encodings(self):
        for text_only in (False, True):
            with self.subTest(text_only=text_only):
                calls = self.run_probe(text_only=text_only)
                self.assertEqual(calls[-1]["method"], "tools/call")

    def test_old_server_version_is_rejected_and_session_is_closed(self):
        with self.assertRaisesRegex(RuntimeError, "version differs"):
            self.run_probe(initialized_version="0.1.0", session=True)
        self.assertEqual(self.calls[-1]["method"], "DELETE")

    def test_missing_wrong_or_mismatched_runtime_identity_is_rejected(self):
        for manifest in ({}, {"package_version": "0.2.0", "build_revision": None},
                         {"package_version": "0.2.0", "build_revision": "b" * 40},
                         {"package_version": "0.1.0", "build_revision": "a" * 40}):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(RuntimeError, "identity differs"):
                self.run_probe(manifest=manifest)

    def test_mcp_errors_never_confirm_identity_even_with_matching_data(self):
        for kwargs in ({"tool_error": True}, {"rpc_error": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.run_probe(**kwargs)

    def test_malformed_manifest_is_rejected(self):
        for manifest in ([], "not an object", 1):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(RuntimeError, "not an object"):
                self.run_probe(manifest=manifest)


class ArtifactIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "noviis-mcp"\nversion = "0.2.0"\n', encoding="utf-8")
        self.build = self.root / "tools/_build_info.py"
        self.build.write_text("PACKAGE_VERSION = '0.2.0'\nBUILD_REVISION = '" + "a" * 40 + "'\n", encoding="utf-8")
        write_manifest(self.root, "a" * 40)

    def test_identity_uses_checked_source_and_not_installed_environment(self):
        (self.root / ".venv").mkdir()
        (self.root / ".venv/extra.txt").write_text("installed files", encoding="utf-8")
        self.assertEqual(read_release_identity(self.root), ReleaseIdentity("0.2.0", "a" * 40))

    def test_tampering_cannot_downgrade_to_legacy_rollback(self):
        self.build.unlink()
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            read_release_identity(self.root, allow_legacy=True)

    def test_identity_constants_must_agree_with_artifact_revision_and_project(self):
        for source in ("PACKAGE_VERSION = '0.2.0'\nBUILD_REVISION = '" + "b" * 40 + "'\n",
                       "PACKAGE_VERSION = '0.1.0'\nBUILD_REVISION = '" + "a" * 40 + "'\n"):
            with self.subTest(source=source):
                self.build.write_text(source, encoding="utf-8")
                write_manifest(self.root, "a" * 40)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    read_release_identity(self.root)

    def test_legacy_metadata_is_allowed_only_for_explicit_rollback(self):
        self.build.unlink()
        write_manifest(self.root, "a" * 40)
        self.assertIsNone(read_release_identity(self.root, allow_legacy=True))
        with self.assertRaisesRegex(ValueError, "missing checksummed"):
            read_release_identity(self.root)

    def test_embedded_source_is_parsed_without_execution(self):
        self.build.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
        write_manifest(self.root, "a" * 40)
        with self.assertRaisesRegex(ValueError, "Invalid embedded"):
            read_release_identity(self.root)
