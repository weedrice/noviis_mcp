from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.annotations import READ_ONLY_TOOLS, WRITE_TOOLS, tool_annotations

with patch("logging_utils.configure_logging"), patch("mcp.server.mcpserver.server.configure_logging"):
    import main


class ToolAnnotationsTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_registered_tool_exposes_explicit_behavior_hints(self):
        definitions = await main.create_mcp_server().list_tools()
        by_name = {tool.name: tool.annotations for tool in definitions}
        self.assertEqual(set(by_name), READ_ONLY_TOOLS | WRITE_TOOLS)
        for name, hints in by_name.items():
            self.assertIsNotNone(hints, name)
            self.assertEqual(hints.read_only_hint, name in READ_ONLY_TOOLS)
            self.assertIsInstance(hints.destructive_hint, bool)
            self.assertIsInstance(hints.idempotent_hint, bool)
            self.assertIsInstance(hints.open_world_hint, bool)
        self.assertTrue(by_name["delete_post"].destructive_hint)
        for name in ("create_post", "create_comment", "create_reply", "send_note", "register_agent", "upload_post_image"):
            self.assertFalse(by_name[name].read_only_hint)
            self.assertFalse(by_name[name].idempotent_hint)
        self.assertFalse(by_name["mark_post_activity_read"].idempotent_hint)
        self.assertFalse(by_name["like_post"].idempotent_hint)
        self.assertTrue(by_name["mark_note_read"].idempotent_hint)
        self.assertTrue(by_name["like_comment"].idempotent_hint)
        self.assertFalse(by_name["get_agent_guide"].open_world_hint)
        self.assertFalse(by_name["get_agent_manifest"].open_world_hint)
        self.assertTrue(by_name["search_content"].open_world_hint)

    def test_new_tools_require_an_explicit_behavior_classification(self):
        with self.assertRaises(ValueError):
            tool_annotations("new_unclassified_tool")
