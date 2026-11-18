from __future__ import annotations

import base64
import unittest

import httpx
from client import NoviIsClient

from mcp.server.mcpserver.exceptions import ToolError

from test_backend_input_contract import setup_context
from tools.home import _to_opportunity_actions
from tools.tool_contract import tool_contracts, validate_action


def upload_args(value):
    return {"filename": "image.png", "mime_type": "image/png",
            "image_base64": value, "agent_token": "synthetic"}


class UploadRecommendationValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_client_validates_mime_before_sending(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"imageFileId": 1})

        async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with self.assertRaisesRegex(ValueError, "mime_type"):
                await client.upload_post_image(token="synthetic", filename="image.png", mime_type="text/html", image_bytes=b"x")
            self.assertEqual(requests, [])
            await client.upload_post_image(token="synthetic", filename="image.png", mime_type=" IMAGE/PNG ", image_bytes=b"x")
            self.assertIn(b"Content-Type: image/png\r\n", requests[0].content)

    async def assert_invalid(self, value):
        args = upload_args(value)
        backend, ctx = setup_context()
        normalized, invalid, missing, warning = validate_action("upload_post_image", args)
        self.assertEqual(invalid, ["image_base64"])
        self.assertEqual(missing, [])
        self.assertEqual(normalized, args)
        self.assertEqual(warning, "Invalid or unsupported params for upload_post_image: image_base64")
        action, = _to_opportunity_actions([{"tool": "upload_post_image", "params": args}])
        self.assertFalse(action.valid)
        self.assertFalse(action.input_complete)
        self.assertEqual(action.invalid_params, ["image_base64"])
        result = await tool_contracts()["upload_post_image"].run(args, context=ctx)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error, "invalid_image_base64")
        self.assertEqual(backend.mock_calls, [])
        self.assertEqual(ctx.request_context.lifespan_context.challenge_manager._challenges, {})

    async def test_malformed_empty_and_invalid_data_uri_are_not_executable_actions(self):
        for value in ("", "   ", "invalid-base64!", "a", "====", "aGVsbG8", "한글",
                      "data:image/png,SGVsbG8=", "data:image/png;base64", "data:image/png;base64,"):
            with self.subTest(value=value):
                await self.assert_invalid(value)

    async def test_decoded_size_above_ten_mib_is_rejected_by_both_paths(self):
        await self.assert_invalid(base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode("ascii"))

    async def test_raw_whitespace_and_data_uri_forms_preserve_upload_bytes(self):
        # This validation checks Base64, not image signatures; the backend owns image validation.
        data = b"synthetic image bytes"
        encoded = base64.b64encode(data).decode("ascii")
        for value in (encoded, "  " + encoded[:4] + " \n" + encoded[4:] + "\t",
                      "data:image/png;base64," + encoded):
            with self.subTest(value=value):
                backend, ctx = setup_context()
                backend.upload_post_image.return_value = {"imageFileId": 1}
                args = upload_args(value)
                normalized, invalid, missing, warning = validate_action("upload_post_image", args)
                self.assertEqual((invalid, missing, warning), ([], [], None))
                self.assertEqual(normalized, args)
                action, = _to_opportunity_actions([{"tool": "upload_post_image", "params": args}])
                self.assertTrue(action.valid)
                self.assertTrue(action.input_complete)
                self.assertEqual(backend.mock_calls, [])
                result = await tool_contracts()["upload_post_image"].run(args, context=ctx)
                self.assertEqual(result.status, "uploaded")
                backend.upload_post_image.assert_awaited_once_with(
                    token="synthetic", filename="image.png", mime_type="image/png", image_bytes=data)

    async def test_exact_decoded_limit_is_accepted(self):
        backend, ctx = setup_context()
        backend.upload_post_image.return_value = {"imageFileId": 1}
        data = b"x" * (10 * 1024 * 1024)
        args = upload_args(base64.b64encode(data).decode("ascii"))
        self.assertEqual(validate_action("upload_post_image", args)[1:], ([], [], None))
        result = await tool_contracts()["upload_post_image"].run(args, context=ctx)
        self.assertEqual(result.status, "uploaded")
        self.assertEqual(backend.upload_post_image.call_args.kwargs["image_bytes"], data)

    async def test_unsupported_mime_is_rejected_before_upload(self):
        for mime in ("text/html", "image/svg+xml", "", "image/png\r\nInjected: value", "image/jpg"):
            with self.subTest(mime=mime):
                args = {**upload_args("eA=="), "mime_type": mime}
                backend, ctx = setup_context()
                self.assertIn("mime_type", validate_action("upload_post_image", args)[1])
                with self.assertRaisesRegex(ToolError, "mime_type"):
                    await tool_contracts()["upload_post_image"].run(args, context=ctx)
                self.assertEqual(backend.mock_calls, [])

    async def test_mismatched_or_invalid_uri_mime_is_rejected(self):
        for value in ("data:image/jpeg;base64,eA==", "data:text/html;base64,eA==",
                      "data:image/png;base64evil,eA==", "data:;base64,eA=="):
            with self.subTest(value=value):
                await self.assert_invalid(value)

    async def test_all_supported_mime_types_are_canonicalized(self):
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            for data in ("eA==", f"DATA:{mime.upper()};charset=utf-8;BASE64,eA=="):
                with self.subTest(mime=mime, data=data):
                    args = {**upload_args(data), "mime_type": f" {mime.upper()} "}
                    normalized, invalid, missing, _ = validate_action("upload_post_image", args)
                    self.assertEqual((invalid, missing), ([], []))
                    self.assertEqual(normalized["mime_type"], mime)
                    backend, ctx = setup_context()
                    backend.upload_post_image.return_value = {"imageFileId": 1}
                    result = await tool_contracts()["upload_post_image"].run(args, context=ctx)
                    self.assertEqual(result.status, "uploaded")
                    self.assertEqual(backend.upload_post_image.call_args.kwargs["mime_type"], mime)

    async def test_missing_and_wrong_type_remain_distinct(self):
        args = upload_args(None)
        del args["image_base64"]
        self.assertEqual(validate_action("upload_post_image", args)[1:], ([], ["image_base64"], None))
        for value in (None, 123, [], {}):
            with self.subTest(value=value):
                backend, ctx = setup_context()
                args = upload_args(value)
                self.assertEqual(validate_action("upload_post_image", args)[1], ["image_base64"])
                with self.assertRaises(ToolError):
                    await tool_contracts()["upload_post_image"].run(args, context=ctx)
                self.assertEqual(backend.mock_calls, [])
