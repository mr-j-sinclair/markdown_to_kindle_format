import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import md_to_kindle as mtk

SAMPLE_MARKDOWN = """# Sample

**Created:** 8/31/2026 9:44:08

Intro paragraph.

1. First ordinary item
2. Second ordinary item

More text before a paren-style list.

1) First paren item
2) Second paren item

```mermaid
flowchart LR
    A[Start] --> B[End]
```
"""


def _all_xhtml_text(epub_path):
    with zipfile.ZipFile(epub_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".xhtml")]
        return "\n".join(zf.read(n).decode("utf-8") for n in names)


def _convert_sample_markdown(output_dir):
    input_path = os.path.join(output_dir, "sample.md")
    output_path = os.path.join(output_dir, "sample.epub")
    with open(input_path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_MARKDOWN)
    mtk.convert(input_path, output_path)
    return output_path


class FlowchartEffectiveDirectionTests(unittest.TestCase):
    """`_flowchart_effective_direction` flips a TB/BT fan-out wider than it
    is deep to LR/RL, so a directory-tree-style diagram (one parent, many
    children) stacks its siblings vertically instead of rendering as an
    extremely wide, short strip. See the "Layout direction" note in
    md_to_kindle.py's Mermaid module banner."""

    def test_wide_fanout_flips_tb_to_lr(self):
        # One parent, five children: fan-out (5) > depth (2).
        edges = [("repo", child, None, "solid", False) for child in "abcde"]
        self.assertEqual(mtk._flowchart_effective_direction("TB", edges), "LR")

    def test_wide_fanout_flips_bt_to_rl(self):
        edges = [("repo", child, None, "solid", False) for child in "abcde"]
        self.assertEqual(mtk._flowchart_effective_direction("BT", edges), "RL")

    def test_deep_chain_stays_tb(self):
        # A --> B --> C --> D --> E: depth (5) >= fan-out (1).
        chain = list("ABCDE")
        edges = [(chain[i], chain[i + 1], None, "solid", False) for i in range(len(chain) - 1)]
        self.assertEqual(mtk._flowchart_effective_direction("TB", edges), "TB")

    def test_branch_and_converge_stays_tb_when_not_wider_than_deep(self):
        # pr -> ci -> {ruff, pytest} -> merge -> cd -> {build, deploy}:
        # max fan-out (2) <= depth (6), so TB is left alone.
        edges = [
            ("pr", "ci", None, "solid", False),
            ("ci", "ruff", None, "solid", False),
            ("ci", "pytest", None, "solid", False),
            ("ruff", "merge", None, "solid", False),
            ("pytest", "merge", None, "solid", False),
            ("merge", "cd", None, "solid", False),
            ("cd", "build", None, "solid", False),
            ("cd", "deploy", None, "solid", False),
        ]
        self.assertEqual(mtk._flowchart_effective_direction("TB", edges), "TB")

    def test_declared_lr_is_never_overridden(self):
        edges = [("repo", child, None, "solid", False) for child in "abcde"]
        self.assertEqual(mtk._flowchart_effective_direction("LR", edges), "LR")

    def test_cycle_leaves_direction_untouched(self):
        edges = [
            ("a", "b", None, "solid", False),
            ("b", "c", None, "solid", False),
            ("c", "a", None, "solid", False),
        ]
        self.assertEqual(mtk._flowchart_effective_direction("TB", edges), "TB")

    def test_wide_nested_tree_renders_taller_than_wide(self):
        # A real fan-out-of-fan-outs shape (repo/ -> 9 children, one of
        # which -- src/ -- itself fans out to 6 more): should still flip
        # to LR since the widest single fan-out (9) exceeds the depth (3).
        source = """flowchart TD
    repo[repo/]
    repo --> src[src/]
    src --> workflows[workflows/]
    src --> agentsdir[agents/]
    src --> tools[tools/]
    src --> models[models/]
    src --> services[services/]
    src --> guardrails[guardrails/]
    repo --> prompts[prompts/]
    repo --> skills[skills/]
    repo --> resources[resources/]
    repo --> evals[evals/]
    repo --> tests[tests/]
    repo --> ui[ui/]
    repo --> docs[docs/]
    repo --> demo[demo/]
"""
        direction, nodes, edges = mtk.parse_mermaid_flowchart(source)
        self.assertEqual(direction, "TB")
        self.assertEqual(mtk._flowchart_effective_direction(direction, edges), "LR")


class ConversionIntegrationTests(unittest.TestCase):
    def test_ordered_lists_and_timestamp_render_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = _convert_sample_markdown(tmp)

            self.assertTrue(os.path.exists(output_path))
            xhtml = _all_xhtml_text(output_path)

            self.assertIn("<ol>", xhtml)
            self.assertGreaterEqual(xhtml.count("<ol>"), 2)
            self.assertIn("2026-08-31 09:44:08", xhtml)

    @unittest.skipUnless(shutil.which("dot"), "graphviz 'dot' binary not on PATH")
    def test_mermaid_diagram_renders_to_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = _convert_sample_markdown(tmp)
            xhtml = _all_xhtml_text(output_path)
            self.assertIn("<img", xhtml)


if __name__ == "__main__":
    unittest.main()
