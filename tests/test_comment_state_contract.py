from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError
from exceptions import BackendContractError
from tools.activity import _build_comments_result
from tools.tool_contract import tool_contracts


def comment(**changes):
    return {"commentId": 1, "postId": 7, "content": "reviewed text", **changes}


def parse(item):
    return _build_comments_result({"data": {"content": [item]}}).comments[0]


class CommentStateContractTest(unittest.TestCase):
    def test_counts_are_nonnegative_json_integers_and_aliases_agree(self):
        for key in ("replyCount", "reply_count", "likeCount", "like_count", "depth"):
            for value in (None, True, -1, 1.5, "1", {}, [], 9223372036854775808):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    parse(comment(**{key:value}))
        for values in ({"replyCount":1,"reply_count":2}, {"likeCount":0,"like_count":1}):
            with self.subTest(values=values), self.assertRaises(BackendContractError):
                parse(comment(**values))
        result = parse(comment(replyCount=2,reply_count=2,likeCount=0,depth=1))
        self.assertEqual((result.reply_count,result.like_count,result.depth),(2,0,1))

    def test_reply_and_deleted_flags_require_json_booleans(self):
        for key in ("hasReplies","has_replies","isDeleted","is_deleted"):
            for value in (None,"unknown","false",0,1,[],{}):
                with self.subTest(key=key,value=value), self.assertRaises(BackendContractError):
                    parse(comment(**{key:value}))
        for values in ({"hasReplies":True,"has_replies":False}, {"isDeleted":True,"is_deleted":False}):
            with self.assertRaises(BackendContractError):parse(comment(**values))

    def test_explicit_reply_count_and_flag_must_agree(self):
        for count,flag in ((0,True),(3,False)):
            with self.subTest(count=count,flag=flag),self.assertRaises(BackendContractError):
                parse(comment(replyCount=count,hasReplies=flag))
        for count,flag in ((0,False),(3,True)):
            result=parse(comment(replyCount=count,hasReplies=flag))
            self.assertEqual((result.reply_count,result.has_replies),(count,flag))
        # Legacy omitted counts can still advertise replies not embedded here.
        self.assertTrue(parse(comment(hasReplies=True)).has_replies)

    def test_embedded_replies_cannot_exceed_the_count_or_conflict_with_false_flag(self):
        children = [comment(commentId=2, parentId=1)]
        for values in ({"hasReplies": False}, {"replyCount": 0},
                       {"replyCount": 0, "hasReplies": False}):
            with self.subTest(values=values), self.assertRaises(BackendContractError):
                parse(comment(children=children, **values))
        result = parse(comment(children=children, replyCount=3, hasReplies=True))
        self.assertEqual(result.reply_count, 3)
        self.assertEqual(len(result.replies), 1)

    def test_unknown_or_malformed_status_never_silently_hides_content(self):
        for status in (None,"","UNKNOWN",False,1,{},[]):
            with self.subTest(status=status),self.assertRaises(BackendContractError):
                parse(comment(status=status))
        self.assertEqual(parse(comment(status="active")).content,"reviewed text")

    def test_explicit_status_and_deletion_flag_must_agree(self):
        for status,deleted in (("ACTIVE",True),("DELETED",False),("BLINDED",True),("BLOCKED_AUTHOR",True)):
            with self.subTest(status=status,deleted=deleted),self.assertRaises(BackendContractError):
                parse(comment(status=status,isDeleted=deleted))
        for status in ("ACTIVE","DELETED","BLINDED","BLOCKED_AUTHOR"):
            result=parse(comment(status=status,is_deleted=status=="DELETED"))
            self.assertEqual(result.content,"reviewed text" if status=="ACTIVE" else "")
            self.assertEqual(result.is_deleted,status=="DELETED")

    def test_legacy_defaults_and_hidden_parent_with_visible_reply_remain_supported(self):
        result=parse(comment())
        self.assertEqual((result.status,result.depth,result.like_count,result.reply_count,result.has_replies),
                         ("ACTIVE",0,0,0,False))
        result=parse(comment(isDeleted=True,children=[comment(commentId=2,parentId=1)]))
        self.assertEqual(result.content,"")
        self.assertEqual((result.status,result.reply_count,result.has_replies),("DELETED",1,True))
        self.assertEqual(result.replies[0].content,"reviewed text")


class CommentStateSDKTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_comment_endpoints_reject_malformed_metadata(self):
        for name,args,parent in (("get_post_comments",{"post_id":7},None),
                                 ("get_comment_replies",{"comment_id":8},8)):
            for invalid in ({"replyCount":1.5},{"status":"UNKNOWN"},{"status":"ACTIVE","isDeleted":True}):
                backend=AsyncMock()
                getattr(backend,name).return_value={"data":{"content":[comment(parentId=parent,**invalid)]}}
                ctx=SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
                with self.subTest(tool=name,invalid=invalid),self.assertRaisesRegex(ToolError,"backend_contract_error"):
                    await tool_contracts()[name].run({"agent_token":"synthetic",**args},context=ctx)

    async def test_nested_invalid_reply_fails_the_page(self):
        with self.assertRaises(BackendContractError):
            parse(comment(children=[comment(commentId=2,parentId=1,hasReplies="unknown")]))
