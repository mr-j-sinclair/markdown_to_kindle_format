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


class ConversionIntegrationTests(unittest.TestCase):
    def test_ordered_lists_and_timestamp_render_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "sample.md")
            output_path = os.path.join(tmp, "sample.epub")
            with open(input_path, "w", encoding="utf-8") as f:
                f.write(SAMPLE_MARKDOWN)

            mtk.convert(input_path, output_path)

            self.assertTrue(os.path.exists(output_path))
            xhtml = _all_xhtml_text(output_path)

            self.assertIn("<ol>", xhtml)
            self.assertGreaterEqual(xhtml.count("<ol>"), 2)
            self.assertIn("2026-08-31 09:44:08", xhtml)

            if shutil.which("dot") is None:
                self.skipTest("graphviz 'dot' binary not on PATH; skipping Mermaid render check")
            self.assertIn("<img", xhtml)


if __name__ == "__main__":
    unittest.main()
