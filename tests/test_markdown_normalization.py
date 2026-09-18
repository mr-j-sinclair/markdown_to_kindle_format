import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import md_to_kindle as mtk


class NormalizeExportTimestampsTests(unittest.TestCase):
    def test_us_locale_m_d_yyyy(self):
        text = "**Created:** 8/31/2026 9:44:08"
        self.assertEqual(
            mtk.normalize_export_timestamps(text),
            "**Created:** 2026-08-31 09:44:08",
        )

    def test_yyyy_m_d(self):
        text = "**Exported:** 2026/9/1 14:05:00"
        self.assertEqual(
            mtk.normalize_export_timestamps(text),
            "**Exported:** 2026-09-01 14:05:00",
        )

    def test_invalid_calendar_date_left_untouched(self):
        text = "**Updated:** 2/30/2026 9:44:08"
        self.assertEqual(mtk.normalize_export_timestamps(text), text)

    def test_ordinary_email_date_left_untouched(self):
        text = "From: someone@example.com\nDate: 8/31/2026 9:44:08"
        self.assertEqual(mtk.normalize_export_timestamps(text), text)

    def test_fenced_code_block_left_untouched(self):
        text = (
            "Some text.\n\n"
            "```text\n"
            "**Created:** 8/31/2026 9:44:08\n"
            "```\n\n"
            "More text.\n"
        )
        self.assertEqual(mtk.normalize_export_timestamps(text), text)


class NormalizeParenOrderedListsTests(unittest.TestCase):
    def test_converts_when_preceded_by_blank_line(self):
        text = "Intro.\n\n1) First\n2) Second\n"
        expected = "Intro.\n\n1. First\n2. Second\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), expected)

    def test_converts_continuing_item(self):
        text = "1) First\n2) Second\n3) Third\n"
        expected = "1. First\n2. Second\n3. Third\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), expected)

    def test_mid_sentence_parenthetical_left_alone(self):
        text = "See step (2) above for details.\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), text)

    def test_ordinary_dot_lists_pass_through(self):
        text = "Intro.\n\n1. First\n2. Second\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), text)

    def test_fenced_code_block_left_untouched(self):
        text = (
            "Intro.\n\n"
            "```text\n"
            "1) not a real list marker in this fence\n"
            "```\n"
        )
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), text)


class InsertMetadataLineBreaksTests(unittest.TestCase):
    def test_splits_run_on_label_lines(self):
        text = "**Author:** Jane\n**Posted:** 2026-01-01\n"
        expected = "**Author:** Jane  \n**Posted:** 2026-01-01\n"
        self.assertEqual(mtk.insert_metadata_line_breaks(text), expected)

    def test_single_label_line_untouched(self):
        text = "**Author:** Jane\n\nSome prose.\n"
        self.assertEqual(mtk.insert_metadata_line_breaks(text), text)

    def test_bulleted_label_sub_item_not_matched(self):
        text = "- **Header:** explanation\n- **Other:** more\n"
        self.assertEqual(mtk.insert_metadata_line_breaks(text), text)

    def test_fenced_code_block_left_untouched(self):
        text = (
            "Intro.\n\n"
            "```text\n"
            "**Author:** Jane\n"
            "**Posted:** 2026-01-01\n"
            "```\n"
        )
        self.assertEqual(mtk.insert_metadata_line_breaks(text), text)


class EnsureBlankLineBeforeBlocksTests(unittest.TestCase):
    def test_inserts_missing_blank_line_before_list(self):
        text = "Some paragraph\n- item one\n- item two\n"
        expected = "Some paragraph\n\n- item one\n- item two\n"
        self.assertEqual(mtk.ensure_blank_line_before_blocks(text), expected)

    def test_inserts_missing_blank_line_before_table(self):
        text = "Some paragraph\n| a | b |\n| - | - |\n"
        expected = "Some paragraph\n\n| a | b |\n| - | - |\n"
        self.assertEqual(mtk.ensure_blank_line_before_blocks(text), expected)

    def test_already_correct_markdown_unchanged(self):
        text = "Some paragraph\n\n- item one\n- item two\n"
        self.assertEqual(mtk.ensure_blank_line_before_blocks(text), text)

    def test_fenced_code_block_left_untouched(self):
        text = (
            "Intro.\n\n"
            "```text\n"
            "Some paragraph\n"
            "- item one\n"
            "```\n"
        )
        self.assertEqual(mtk.ensure_blank_line_before_blocks(text), text)


if __name__ == "__main__":
    unittest.main()
