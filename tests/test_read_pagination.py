import unittest

from exceptions import BackendContractError
from tools.pagination import read_pagination


class ReadPaginationTests(unittest.TestCase):
    def test_absent_metadata_stays_unknown_even_with_request_context(self):
        self.assertEqual(read_pagination({}, requested_page=2, requested_size=20), {
            "page": None, "size": None, "total_elements": None, "total_pages": None,
            "has_next": None, "is_last": None,
        })

    def test_integer_fields_reject_coercions_null_and_negative_values(self):
        for key in ("page", "number", "pageNumber", "size", "pageSize", "total_elements", "totalElements", "total_pages", "totalPages"):
            for value in (None, True, False, 1.0, "1", -1, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    read_pagination({key: value})
        for key in ("size", "pageSize"):
            with self.subTest(key=key), self.assertRaises(BackendContractError):
                read_pagination({key: 0})

    def test_boolean_fields_require_actual_json_booleans(self):
        for key in ("has_next", "hasNext", "is_last", "isLast", "last"):
            for value in (None, "false", "true", 0, 1, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    read_pagination({key: value})

    def test_aliases_must_agree(self):
        for data in ({"page": 1, "number": 2}, {"number": 1, "pageNumber": 2},
                     {"size": 20, "pageSize": 10}, {"total_elements": 3, "totalElements": 4},
                     {"total_pages": 1, "totalPages": 2}, {"has_next": True, "hasNext": False},
                     {"last": True, "isLast": False}):
            with self.subTest(data=data), self.assertRaises(BackendContractError):
                read_pagination(data)
        self.assertEqual(read_pagination({"page": 1, "number": 1, "pageNumber": 1})["page"], 1)

    def test_requested_page_and_size_are_checked_only_when_present(self):
        for data in ({"page": 0}, {"size": 10}):
            with self.subTest(data=data), self.assertRaises(BackendContractError):
                read_pagination(data, requested_page=2, requested_size=20)
        read_pagination({"page": 2, "size": 20}, requested_page=2, requested_size=20)

    def test_total_pages_matches_ceiling_without_float_rounding(self):
        for elements, size, pages in ((0, 20, 0), (20, 20, 1), (21, 20, 2), (10**30 + 1, 10, 10**29 + 1)):
            self.assertEqual(read_pagination({"size": size, "totalElements": elements, "totalPages": pages})["total_pages"], pages)
            with self.assertRaises(BackendContractError):
                read_pagination({"size": size, "totalElements": elements, "totalPages": pages + 1})

    def test_last_and_has_next_must_be_opposites(self):
        for flag in (False, True):
            with self.assertRaises(BackendContractError):
                read_pagination({"last": flag, "hasNext": flag})
        self.assertFalse(read_pagination({"last": True})["has_next"])
        self.assertTrue(read_pagination({"hasNext": False})["is_last"])

    def test_status_matches_page_totals_without_inventing_missing_flags(self):
        for data in ({"page": 0, "totalPages": 2, "hasNext": False},
                     {"page": 1, "totalPages": 2, "last": False},
                     {"page": 0, "totalPages": 0, "hasNext": True}):
            with self.subTest(data=data), self.assertRaises(BackendContractError):
                read_pagination(data)
        result = read_pagination({"page": 0, "totalPages": 2})
        self.assertIsNone(result["has_next"])
        self.assertIsNone(result["is_last"])

    def test_out_of_range_empty_pages_are_valid(self):
        result = read_pagination({"content": [], "page": 99, "size": 20,
                                  "total_elements": 21, "total_pages": 2, "last": True},
                                 requested_page=99, requested_size=20)
        self.assertEqual(result["page"], 99)
        self.assertFalse(result["has_next"])
        read_pagination({"content": [], "page": 3, "size": 20, "total_elements": 0, "total_pages": 0, "last": True})


if __name__ == "__main__":
    unittest.main()
