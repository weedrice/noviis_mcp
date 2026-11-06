from __future__ import annotations

import unittest

from mcp.server.fastmcp.exceptions import ToolError

from test_read_resource_contract import comment, post, run_read


class CommentContentValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_active_content_requires_nonempty_text_on_both_endpoints(self):
        for tool, args, parent in (("get_post_comments", {"post_id": 11}, None),
                                   ("get_comment_replies", {"comment_id": 20}, 20)):
            for value in (None, False, 123, {}, [], "", "   "):
                with self.subTest(tool=tool, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await run_read(tool, [comment(content=value, parentId=parent)], args)
            item=comment(parentId=parent)
            del item["content"]
            with self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read(tool, [item], args)

    async def test_body_alias_and_exact_text_are_preserved(self):
        text="  Original Unicode 본문\nwith whitespace  "
        for source in ({"content": text}, {"body": text}, {"content": text, "body": text}):
            item=comment()
            del item["content"]
            item.update(source)
            result=await run_read("get_post_comments", [item], {"post_id":11})
            self.assertEqual(result.comments[0].content,text)

    async def test_conflicting_aliases_fail_without_echoing_content(self):
        for body in ("other private text", None, 123):
            with self.subTest(body=body), self.assertRaises(ToolError) as caught:
                await run_read("get_post_comments", [comment(content="private comment",body=body)], {"post_id":11})
            self.assertNotIn("private comment",str(caught.exception))
            self.assertNotIn("other private text",str(caught.exception))

    async def test_nested_active_body_corruption_fails_the_whole_page(self):
        root=comment(children=[comment(commentId=22,parentId=21,content=None)])
        with self.assertRaisesRegex(ToolError,"backend_contract_error"):
            await run_read("get_post_comments",[root],{"post_id":11})

    async def test_hidden_placeholders_do_not_require_or_expose_content(self):
        for status in ("DELETED","BLINDED","BLOCKED_AUTHOR"):
            for source in ({}, {"content":None}, {"content":{"private":"body"},"body":"different"}):
                item=comment(status=status,children=[comment(commentId=22,parentId=21)])
                del item["content"]
                item.update(source)
                result=await run_read("get_post_comments",[item],{"post_id":11})
                self.assertEqual(result.comments[0].content,"")
                self.assertEqual(result.comments[0].replies[0].content,"reviewed comment")


class CategoryReadValidationTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, fields, category_id="003"):
        args={"board_id":7}
        if category_id is not None:
            args["category_id"]=category_id
        return await run_read("get_board_posts",[post(**fields)],args)

    async def test_explicit_different_or_uncategorized_results_are_rejected(self):
        for fields in ({"categoryId":99}, {"category_id":99}, {"category":{"categoryId":99}},
                       {"category":{"id":99}}, {"categoryId":None}, {"category":None},
                       {"category":{"category_id":None}}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ToolError,"backend_contract_error"):
                await self.call(fields)

    async def test_matching_top_level_nested_and_agreeing_aliases_are_supported(self):
        for fields in ({"categoryId":3}, {"category_id":3}, {"category":{"id":3,"name":"Category"}},
                       {"categoryId":3,"category_id":3,"category":{"categoryId":3,"category_id":3,"id":3}}):
            result=await self.call(fields)
            self.assertEqual(result.posts[0].category_id,"3")

    async def test_malformed_identity_and_conflicting_aliases_fail_even_without_filter(self):
        cases=[{"categoryId":value} for value in (True,0,-1,3.0,"3",[],{},9223372036854775808)]
        cases += [{"categoryId":3,"category_id":4}, {"categoryId":3,"category":{"categoryId":4}},
                  {"categoryId":3,"category":None}, {"category":{"categoryId":3,"id":4}}]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(ToolError,"backend_contract_error"):
                await self.call(fields,category_id=None)

    async def test_missing_legacy_identity_stays_unknown_and_unfiltered_null_is_valid(self):
        for fields in ({}, {"category":"Legacy category"}, {"category":{"name":"Legacy category"}}):
            result=await self.call(fields)
            self.assertIsNone(result.posts[0].category_id)
        result=await self.call({"category":None},category_id=None)
        self.assertIsNone(result.posts[0].category_id)
        result=await self.call({"category":{"categoryId":99}},category_id=None)
        self.assertEqual(result.posts[0].category_id,"99")

    async def test_empty_filtered_page_is_valid(self):
        result=await run_read("get_board_posts",[],{"board_id":7,"category_id":3})
        self.assertEqual(result.posts,[])
