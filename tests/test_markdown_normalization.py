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

    def test_converts_backslash_escaped_paren(self):
        text = "Intro.\n\n1\\) First\n2\\) Second\n"
        expected = "Intro.\n\n1. First\n2. Second\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), expected)

    def test_converts_run_directly_after_prose_line(self):
        text = "Explain to me what\n1\\) First\n2\\) Second\n"
        expected = "Explain to me what\n1. First\n2. Second\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), expected)

    def test_lone_item_after_prose_line_left_alone(self):
        text = "A wrapped sentence ending\n2) with a lone marker.\n"
        self.assertEqual(mtk.normalize_paren_ordered_lists(text), text)

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


ZW = "​"


class StripZeroWidthFenceMarkersTests(unittest.TestCase):
    def test_gemini_split_fence_rejoined(self):
        text = f"`{ZW}`{ZW}`mermaid\ngraph LR\n  A --> B\n`{ZW}`{ZW}`\n"
        expected = "```mermaid\ngraph LR\n  A --> B\n```\n"
        self.assertEqual(mtk.strip_zero_width_fence_markers(text), expected)

    def test_blockquoted_fence_rejoined(self):
        text = f"> `{ZW}`{ZW}`mermaid\n> graph LR\n> `{ZW}`{ZW}`\n"
        expected = "> ```mermaid\n> graph LR\n> ```\n"
        self.assertEqual(mtk.strip_zero_width_fence_markers(text), expected)

    def test_zero_width_in_prose_left_untouched(self):
        text = f"word{ZW}joiner and inline `{ZW}code` here\n"
        self.assertEqual(mtk.strip_zero_width_fence_markers(text), text)


class EnsureBlankLinesAroundFencesTests(unittest.TestCase):
    def test_list_after_closing_fence_separated(self):
        text = "```\nx\n```\n- a\n- b\n"
        expected = "```\nx\n```\n\n- a\n- b\n"
        self.assertEqual(mtk.ensure_blank_lines_around_fences(text), expected)

    def test_blockquoted_fence_keeps_quote_prefix(self):
        text = "> ```mermaid\n> graph LR\n> ```\n> *   item\n"
        expected = "> ```mermaid\n> graph LR\n> ```\n>\n> *   item\n"
        self.assertEqual(mtk.ensure_blank_lines_around_fences(text), expected)

    def test_already_spaced_unchanged(self):
        text = "Intro\n\n```\nx\n```\n\nAfter\n"
        self.assertEqual(mtk.ensure_blank_lines_around_fences(text), text)

    def test_fence_after_list_item_separated(self):
        text = "> *   item\n> ```mermaid\n> graph LR\n> ```\n"
        expected = "> *   item\n>\n> ```mermaid\n> graph LR\n> ```\n"
        self.assertEqual(mtk.ensure_blank_lines_around_fences(text), expected)

    def test_fence_inside_list_item_unchanged(self):
        text = "- a\n  ```\n  x\n  ```\n- b\n"
        self.assertEqual(mtk.ensure_blank_lines_around_fences(text), text)


class NormalizeNestedListIndentTests(unittest.TestCase):
    def test_gemini_four_space_children_reindented(self):
        text = "*   **A:**\n    *   detail\n*   **B:**\n    *   more\n"
        expected = "*   **A:**\n  *   detail\n*   **B:**\n  *   more\n"
        self.assertEqual(mtk.normalize_nested_list_indent(text), expected)

    def test_blockquoted_children_reindented(self):
        text = "> *   **A:**\n>     *   detail\n"
        expected = "> *   **A:**\n>   *   detail\n"
        self.assertEqual(mtk.normalize_nested_list_indent(text), expected)

    def test_deeper_descendants_shift_with_parent(self):
        text = "*   a\n    *   b\n        *   c\n    *   d\n"
        expected = "*   a\n  *   b\n    *   c\n  *   d\n"
        self.assertEqual(mtk.normalize_nested_list_indent(text), expected)

    def test_already_nesting_lists_unchanged(self):
        text = "- a\n  - b\n    - c\n1. x\n   - y\n"
        self.assertEqual(mtk.normalize_nested_list_indent(text), text)

    def test_fenced_code_block_left_untouched(self):
        text = "- a\n```\n- a\n    - b\n```\n"
        self.assertEqual(mtk.normalize_nested_list_indent(text), text)


if __name__ == "__main__":
    unittest.main()
