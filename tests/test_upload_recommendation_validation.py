from __future__ import annotations

import base64
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from test_backend_input_contract import setup_context
from tools.home import _to_opportunity_actions
from tools.tool_contract import tool_contracts, validate_action


def upload_args(value):
    return {"filename": "image.png", "mime_type": "image/png",
            "image_base64": value, "agent_token": "synthetic"}


class UploadRecommendationValidationTest(unittest.IsolatedAsyncioTestCase):
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
