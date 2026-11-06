import unittest

from exceptions import BackendContractError
from tools.activity import _build_comments_result, _build_feed_result
from tools.notes import build_note_thread_result, build_notes_result


class ReadResponseContractTests(unittest.TestCase):
    builders = (_build_feed_result, _build_comments_result, build_notes_result, build_note_thread_result)

    def test_corrupt_collection_never_becomes_empty_page(self):
        for builder in self.builders:
            for payload in (
                None, [], {}, {"success": True, "data": None},
                {"data": []}, {"data": {}},
                {"data": {"content": "unexpected"}},
                {"data": {"content": [None]}},
                {"data": {"content": [{}, "unexpected"]}},
                {"success": False, "data": {"content": []}},
                {"error": {"code": "upstream_failure"}, "content": []},
            ):
                with self.subTest(builder=builder.__name__, payload=payload):
                    with self.assertRaises(BackendContractError):
                        builder(payload)

    def test_explicit_empty_pages_and_optional_metadata_are_valid(self):
        for builder in self.builders:
            for envelope in (False, True):
                with self.subTest(builder=builder.__name__, envelope=envelope):
                    data = {"content": [], "future_optional_field": True}
                    if builder is build_note_thread_result:
                        data["note_thread_id"] = 7
                    result = builder({"success": True, "data": data} if envelope else data)
                    collection = next(getattr(result, name) for name in ("posts", "comments", "content", "notes") if hasattr(result, name))
                    self.assertEqual([], collection)

    def test_supported_collection_aliases_are_preserved(self):
        for builder, aliases in (
            (_build_feed_result, ("posts",)),
            (_build_comments_result, ("comments",)),
            (build_notes_result, ("notes", "threads")),
            (build_note_thread_result, ("notes",)),
        ):
            for alias in aliases:
                with self.subTest(builder=builder.__name__, alias=alias):
                    data = {alias: []}
                    if builder is build_note_thread_result:
                        data["note_thread_id"] = 7
                    builder({"data": data})

    def test_corrupt_comment_descendants_fail_instead_of_disappearing(self):
        for children in (None, {}, [None], [{"commentId": 2, "postId": 7, "parentId": 1, "children": "unexpected"}]):
            with self.subTest(children=children):
                with self.assertRaises(BackendContractError):
                    _build_comments_result({"data": {"content": [{"commentId": 1, "postId": 7, "children": children}]}})


if __name__ == "__main__":
    unittest.main()
