from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.annotations import READ_ONLY_TOOLS, WRITE_TOOLS, tool_annotations

with patch("logging_utils.configure_logging"), patch("mcp.server.fastmcp.server.configure_logging"):
    import main


class ToolAnnotationsTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_registered_tool_exposes_explicit_behavior_hints(self):
        definitions = await main.create_mcp_server().list_tools()
        by_name = {tool.name: tool.annotations for tool in definitions}
        self.assertEqual(set(by_name), READ_ONLY_TOOLS | WRITE_TOOLS)
        for name, hints in by_name.items():
            self.assertIsNotNone(hints, name)
            self.assertEqual(hints.readOnlyHint, name in READ_ONLY_TOOLS)
            self.assertIsInstance(hints.destructiveHint, bool)
            self.assertIsInstance(hints.idempotentHint, bool)
            self.assertIsInstance(hints.openWorldHint, bool)
        self.assertTrue(by_name["delete_post"].destructiveHint)
        for name in ("create_post", "create_comment", "create_reply", "send_note", "register_agent", "upload_post_image"):
            self.assertFalse(by_name[name].readOnlyHint)
            self.assertFalse(by_name[name].idempotentHint)
        self.assertFalse(by_name["mark_post_activity_read"].idempotentHint)
        self.assertFalse(by_name["like_post"].idempotentHint)
        self.assertTrue(by_name["mark_note_read"].idempotentHint)
        self.assertTrue(by_name["like_comment"].idempotentHint)
        self.assertFalse(by_name["get_agent_guide"].openWorldHint)
        self.assertFalse(by_name["get_agent_manifest"].openWorldHint)
        self.assertTrue(by_name["search_content"].openWorldHint)

    def test_new_tools_require_an_explicit_behavior_classification(self):
        with self.assertRaises(ValueError):
            tool_annotations("new_unclassified_tool")
