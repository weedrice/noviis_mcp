from __future__ import annotations

import unittest

from exceptions import BackendContractError
from tools.activity import _to_board
from tools.home import _to_opportunity_actions
from tools.parsing import guidance_texts
from tools.tool_contract import validate_action


class ContractConsistencyTest(unittest.TestCase):
    def test_explicit_board_total_wins_over_recent_sample(self):
        for field in ("postCount", "post_count"):
            self.assertEqual(_to_board({field: 100, "latestPosts": [{"id": 1}]}).post_count, 100)
            self.assertEqual(_to_board({field: 0}).post_count, 0)
        for value in (None, True, -1, "100", 1.5):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                _to_board({"postCount": value})
        with self.assertRaises(BackendContractError):
            _to_board({"latestPosts": []})

    def test_guidance_keeps_string_contract_and_extracts_backend_text(self):
        self.assertEqual(guidance_texts(["legacy", {"code": "quality", "text": "Helpful contributions"},
                                        {"description": "Clear tone"}]),
                         ["legacy", "Helpful contributions", "Clear tone"])
        self.assertEqual(guidance_texts([]), [])
        for value in (None, {}, [{"code": "missing"}], [1], [None]):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                guidance_texts(value)

    def test_actions_normalize_ids_and_distinguish_missing_inputs(self):
        source = [{"tool": "get_post_comments", "params": {"post_id": 170}},
                  {"tool": "create_comment", "params": {"post_id": 170}}]
        read, write = _to_opportunity_actions(source)
        self.assertEqual(read.params["post_id"], "170")
        self.assertTrue(read.valid)
        self.assertTrue(read.input_complete)
        self.assertEqual(read.missing_params, [])
        self.assertTrue(write.valid)
        self.assertFalse(write.input_complete)
        self.assertEqual(write.missing_params, ["content"])
        self.assertEqual(source[0]["params"]["post_id"], 170)

    def test_actions_reject_invalid_types_ranges_ids_and_unknown_fields(self):
        cases = [({"post_id": True}, "post_id"), ({"post_id": 1.5}, "post_id"),
                 ({"post_id": 0}, "post_id"), ({"post_id": "../secret"}, "post_id"),
                 ({"post_id": None}, "post_id"), ({"post_id": "170", "page": "1"}, "page"),
                 ({"post_id": "170", "size": 101}, "size"),
                 ({"post_id": "170", "unknown": "private-value"}, "unknown")]
        for params, field in cases:
            with self.subTest(params=params):
                action, = _to_opportunity_actions([{"tool": "get_post_comments", "params": params}])
                self.assertFalse(action.valid)
                self.assertFalse(action.input_complete)
                self.assertIn(field, action.invalid_params)
                self.assertNotIn("private-value", action.validation_warning)
        _, invalid, _, warning = validate_action("not_a_tool", {})
        self.assertEqual(invalid, [])
        self.assertIsNotNone(warning)

    def test_operation_uuid_contract_is_not_relaxed_by_numeric_resource_ids(self):
        _, invalid, _, _ = validate_action("get_operation_result", {"operation_id": 170})
        self.assertEqual(invalid, ["operation_id"])

    def test_feed_recommendations_obey_actual_pagination_rules(self):
        for params, expected in (({"cursor": "unsupported"}, ["cursor"]),
                                 ({"limit": 5, "size": 10}, ["limit", "size"])):
            action, = _to_opportunity_actions([{"tool": "get_feed", "params": params}])
            self.assertFalse(action.valid)
            self.assertEqual(action.invalid_params, expected)

    def test_image_recommendations_share_post_validation_and_missing_attachment_rule(self):
        params = {"title": "sample", "content": "sample", "board_id": 9, "image_alt": "description"}
        incomplete, = _to_opportunity_actions([{"tool": "create_post", "params": params}])
        self.assertTrue(incomplete.valid)
        self.assertFalse(incomplete.input_complete)
        self.assertEqual(incomplete.missing_params, ["image_file_id"])
        for extra, field in (({"image_file_id": "bad"}, "image_file_id"),
                             ({"image_alt": "x" * 301}, "image_alt")):
            action, = _to_opportunity_actions([{"tool": "create_post", "params": {**params, **extra}}])
            self.assertFalse(action.valid)
            self.assertIn(field, action.invalid_params)

    def test_partial_actions_expose_alternative_target_and_paired_challenge_inputs(self):
        post, = _to_opportunity_actions([{"tool": "create_post", "params": {"title": "sample", "content": "sample"}}])
        self.assertTrue(post.valid)
        self.assertEqual(post.missing_params, ["board_url"])
        reply, = _to_opportunity_actions([{"tool": "create_reply", "params": {"comment_id": 1, "content": "sample", "answer": "2"}}])
        self.assertEqual(reply.missing_params, ["challenge_id"])
        malformed, = _to_opportunity_actions([{"tool": "get_feed", "params": []}])
        self.assertFalse(malformed.valid)
        self.assertEqual(malformed.invalid_params, ["params"])
        empty, = _to_opportunity_actions([{"tool": "create_comment", "params": {"post_id": 1, "content": " "}}])
        self.assertFalse(empty.valid)
        self.assertIn("content", empty.invalid_params)
