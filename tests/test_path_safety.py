from __future__ import annotations

import unittest
import httpx

from client import NoviIsClient


class PathSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_resource_paths_reject_path_syntax_before_http(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})

        cases = [
            ("get_board_posts", "board_id", {}),
            ("get_post_comments", "post_id", {}),
            ("mark_post_activity_read", "post_id", {}),
            ("delete_post", "post_id", {}),
            ("create_comment", "post_id", {"content": "sample"}),
            ("create_reply", "comment_id", {"content": "sample"}),
            ("like_post", "post_id", {}),
            ("like_comment", "comment_id", {}),
            ("get_note_thread", "note_thread_id", {}),
            ("mark_note_read", "note_thread_id", {}),
        ]
        invalid = ["../../users/123", "/admin", "..", "a/b", "a\\b", "a?x=1", "a#fragment",
                   "%2e%2e", "%252f", "https://example.test", "a\n", " 1", "", "x" * 129,
                   "１", 0, -1, True, None]
        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            for method, key, extra in cases:
                for value in invalid:
                    with self.subTest(method=method, value=value), self.assertRaises(ValueError):
                        await getattr(client, method)(token="test-token", **{key: value}, **extra)
        self.assertEqual(calls, [])

    async def test_numeric_and_opaque_ids_preserve_exact_endpoint(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(204)

        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            for value in (123, "123", "thread-1", "uuid_ABC-123"):
                await client.delete_post(token="test-token", post_id=value)
        self.assertEqual(paths, ["/api/v1/agents/posts/" + str(value) for value in (123, "123", "thread-1", "uuid_ABC-123")])
