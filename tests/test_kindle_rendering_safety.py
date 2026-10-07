"""Kindle rendering safety: text diagrams become images, unrenderable
symbols become text, and no auto title/ToC page precedes the content."""
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import md_to_kindle as mtk

BOX_DIAGRAM = """\
            Your application
                   │
        ┌──────────┴──────────┐
        │                     │
     Pydantic                DSPy
        │                     │
        └──────────┬──────────┘
                   ▼
              Model/provider
"""

DOC = f"""# DSPy and Pydantic & JEV

```text
{BOX_DIAGRAM}```

```text
API
├── PostgreSQL
└── Redis
```

```text
X → Y
```

```python
x = "a --> b"
y = "c --> d"
```

| Need | Pydantic | DSPy |
|---|---|---|
| Validate | ✅ Excellent | ❌ |
| Simple | **✅ Usually enough** | ⚠️ Maybe |
"""


def _convert(md_text, **kwargs):
    tmp = tempfile.mkdtemp()
    in_path = os.path.join(tmp, "dspy_and_pydantic_and_jev.md")
    out_path = os.path.join(tmp, "out.epub")
    with open(in_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    mtk.convert(in_path, out_path, **kwargs)
    zf = zipfile.ZipFile(out_path)
    files = {n: zf.read(n) for n in zf.namelist()}
    return files


def _chapters(files):
    return "\n".join(v.decode("utf-8") for k, v in sorted(files.items())
                     if k.startswith("EPUB/chap_"))


class TextDiagramDetectionTests(unittest.TestCase):
    def test_box_drawing_is_diagram(self):
        self.assertTrue(mtk.is_text_diagram(BOX_DIAGRAM))

    def test_ascii_connector_lines_are_diagram(self):
        self.assertTrue(mtk.is_text_diagram("A\n  |\n  v\nB\n  |\n  v\nC"))

    def test_prose_is_not_diagram(self):
        self.assertFalse(mtk.is_text_diagram("AI is a primitive.\n\nInside the workflow."))

    def test_render_returns_png(self):
        png = mtk.render_text_diagram_image(BOX_DIAGRAM)
        self.assertTrue(png.startswith(b"\x89PNG"))


class KindleSafeSymbolTests(unittest.TestCase):
    def test_status_symbols_become_text(self):
        self.assertEqual(mtk.kindle_safe_symbols("✅ Excellent"), "[Yes] Excellent")
        self.assertEqual(mtk.kindle_safe_symbols("❌"), "[No]")
        self.assertEqual(mtk.kindle_safe_symbols("⚠️ Maybe"), "[Warning] Maybe")
        self.assertEqual(mtk.kindle_safe_symbols("✓done"), "[Yes] done")

    def test_plain_text_untouched(self):
        self.assertEqual(mtk.kindle_safe_symbols("X → Y ≠ Z"), "X → Y ≠ Z")


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = _convert(DOC)
        cls.xhtml = _chapters(cls.files)

    def test_box_diagrams_rendered_as_images(self):
        self.assertEqual(self.xhtml.count('class="content-img text-diagram-img"'), 2)
        pre_blocks = [seg.split("</pre>")[0] for seg in self.xhtml.split("<pre")[1:]]
        self.assertFalse(any("┌" in b or "├" in b for b in pre_blocks))

    def test_small_arrow_block_and_tagged_code_stay_text(self):
        self.assertIn("X → Y", self.xhtml)
        self.assertIn("a --&gt; b", self.xhtml)

    def test_table_symbols_replaced(self):
        self.assertIn("[Yes] Excellent", self.xhtml)
        self.assertIn("<strong>[Yes] Usually enough</strong>", self.xhtml)
        self.assertIn("[Warning] Maybe", self.xhtml)
        self.assertNotIn("✅", self.xhtml)
        self.assertNotIn("❌", self.xhtml)

    def test_no_title_page_and_toc_not_first(self):
        opf = self.files["EPUB/content.opf"].decode("utf-8")
        self.assertNotIn("EPUB/titlepage.xhtml", self.files)
        self.assertIn('properties="nav"', opf)  # still in the manifest
        self.assertNotIn('<itemref idref="nav"', opf)  # but not a page
        self.assertIn("<dc:title>DSPy and Pydantic &amp; JEV</dc:title>", opf)

    def test_title_heading_added_when_no_leading_h1(self):
        files = _convert("Just a paragraph.\n\n## Section\n\nText.\n", subtitle="Sub")
        self.assertNotIn("EPUB/titlepage.xhtml", files)
        xhtml = _chapters(files)
        self.assertRegex(xhtml, r'<h1>dspy and pydantic and jev</h1>\s*<p class="subtitle">Sub</p>')
        self.assertLess(xhtml.index("<h1>"), xhtml.index("Just a paragraph."))


class VersionedTitleTests(unittest.TestCase):
    def test_version_suffix_added_to_title(self):
        self.assertEqual(mtk.versioned_book_title("DSPy", "outputs/dspy_V2.epub"), "DSPy (V2)")
        self.assertEqual(mtk.versioned_book_title("DSPy", "outputs/dspy_v3.epub"), "DSPy (V3)")

    def test_no_suffix_without_version(self):
        self.assertEqual(mtk.versioned_book_title("DSPy", "outputs/dspy.epub"), "DSPy")
        self.assertEqual(mtk.versioned_book_title("Step V2", "outputs/stepV2.epub"), "Step V2")

    def test_not_doubled_when_title_already_versioned(self):
        self.assertEqual(mtk.versioned_book_title("DSPy (V2)", "dspy_V2.epub"), "DSPy (V2)")

    def test_epub_metadata_title_versioned(self):
        tmp = tempfile.mkdtemp()
        in_path = os.path.join(tmp, "doc.md")
        out_path = os.path.join(tmp, "doc_V2.epub")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write("# My Doc\n\nText.\n")
        mtk.convert(in_path, out_path)
        opf = zipfile.ZipFile(out_path).read("EPUB/content.opf").decode("utf-8")
        self.assertIn("<dc:title>My Doc (V2)</dc:title>", opf)
        self.assertIn("<h1>My Doc</h1>", _chapters({k: zipfile.ZipFile(out_path).read(k)
                                                    for k in zipfile.ZipFile(out_path).namelist()}))


if __name__ == "__main__":
    unittest.main()
