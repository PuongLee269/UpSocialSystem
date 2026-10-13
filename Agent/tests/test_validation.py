import unittest

from ytagent.validation import (tags_total_chars, validate_content, validate_description,
                                validate_tags, validate_title)


class TitleTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_title("Xin chào"), [])
        self.assertEqual(validate_title("a" * 100), [])

    def test_invalid(self):
        for bad in ("", "   ", None, "a" * 101, "a<b", "a>b"):
            with self.subTest(bad=bad):
                self.assertTrue(validate_title(bad))


class DescriptionTests(unittest.TestCase):
    def test_byte_limit_not_char_limit(self):
        self.assertEqual(validate_description("a" * 5000), [])
        self.assertTrue(validate_description("a" * 5001))
        self.assertTrue(validate_description("ệ" * 1700))  # 3 byte/ký tự > 5000
        self.assertEqual(validate_description(""), [])

    def test_angle_brackets(self):
        self.assertTrue(validate_description("a <b>"))


class TagTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_tags(["a", "b c"]), [])
        self.assertEqual(validate_tags([]), [])

    def test_empty_duplicate_case_insensitive(self):
        self.assertTrue(validate_tags(["a", ""]))
        self.assertTrue(validate_tags(["Abc", "abc"]))

    def test_tag_too_long(self):
        self.assertTrue(validate_tags(["a" * 101]))
        self.assertEqual(validate_tags(["a" * 100]), [])

    def test_total_counts_quotes_for_spaces(self):
        self.assertEqual(tags_total_chars(["ab", "c d"]), 2 + 3 + 2)
        tags = [f"{i:02d}" + "x" * 46 + " " for i in range(10)]  # 49 + 2 = 51 mỗi tag -> 510
        self.assertTrue(any("Tổng" in e for e in validate_tags(tags)))

    def test_total_exactly_500_ok_and_501_fails(self):
        self.assertEqual(validate_tags(["a" * 99 + str(i) for i in range(5)]), [])
        self.assertTrue(validate_tags(["a" * 99 + str(i) for i in range(5)] + ["b"]))

    def test_not_list(self):
        self.assertTrue(validate_tags("abc"))


class ContentTests(unittest.TestCase):
    def test_aggregates(self):
        self.assertEqual(validate_content({"title": "t", "description": "", "tags": []}), [])
        self.assertEqual(len(validate_content({"title": "", "description": "<", "tags": ["a", "a"]})), 3)


if __name__ == "__main__":
    unittest.main()
