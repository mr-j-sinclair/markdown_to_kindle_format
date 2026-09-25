import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import md_to_kindle as mtk
from bs4 import BeautifulSoup

HAS_EMOJI_FONT = os.path.exists(mtk._PDF_EMOJI_FONT_PATH)


def _pdf_markup(html):
    soup = BeautifulSoup(html, "html.parser")
    return mtk._pdf_inline_markup(soup.p.children)


class PdfGlyphFallbackTests(unittest.TestCase):
    def test_plain_and_covered_text_is_just_escaped(self):
        text = "Stake ≥ 20% → GAP & Tax – £50 “quoted”"
        self.assertEqual(mtk._pdf_text_markup(text), mtk.escape_x(text))

    def test_variation_selector_is_dropped(self):
        self.assertNotIn("️", mtk._pdf_text_markup("⚠️ Written"))

    @unittest.skipUnless(HAS_EMOJI_FONT, "Apple Color Emoji not installed")
    def test_emoji_in_body_text_becomes_inline_image(self):
        markup = _pdf_markup("<p>✅ Built, ⚠️ Written, ❌</p>")
        self.assertEqual(markup.count("<img "), 3)
        self.assertIn("Built, ", markup)
        for ch in "✅⚠❌️":
            self.assertNotIn(ch, markup)

    @unittest.skipUnless(HAS_EMOJI_FONT, "Apple Color Emoji not installed")
    def test_inline_code_emoji_is_image_without_backcolor(self):
        markup = _pdf_markup("<p>e.g. <code>Review ⏳</code></p>")
        self.assertIn("<img ", markup)
        self.assertNotIn("backColor", markup)

    def test_inline_code_without_fallback_keeps_backcolor(self):
        markup = _pdf_markup("<p><code>stake → teams</code></p>")
        self.assertIn('backColor="#f4f6fa"', markup)
        self.assertNotIn("<img ", markup)

    def test_check_mark_in_mono_font_falls_back_to_helvetica(self):
        # Monaco lacks ✓ and the emoji font has no ✓ either; Helvetica's
        # ZapfDingbats substitution does.
        if mtk._pdf_font_has_char(mtk._PDF_MONO_FONT_NAME, "✓"):
            self.skipTest("mono font already covers ✓")
        markup = mtk._pdf_text_markup("Extract ✓", mtk._PDF_MONO_FONT_NAME)
        self.assertIn('<font face="Helvetica">✓</font>', markup)


class PdfListCodeBlockTests(unittest.TestCase):
    def test_fenced_code_in_list_item_is_a_real_code_block(self):
        from reportlab.platypus import Preformatted, Table
        md = ("- GitHub:\n"
              "  - To publish:\n\n"
              "    ```bash\n"
              "    cd repo\n"
              "    git push\n"
              "    ```\n\n"
              "  - Check status.\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list_code.md")
            with open(path, "w") as f:
                f.write(md)
            soup, _ = mtk.load_markdown(path)
        renderer = mtk.PdfRenderer(mtk.build_pdf_styles(), mtk.ImageRegistry())
        flowables = renderer.render_list(soup.find("ul"))
        pres = [cell for fl in flowables if isinstance(fl, Table)
                for row in fl._cellvalues for cell in row if isinstance(cell, Preformatted)]
        self.assertEqual(len(pres), 1)
        self.assertEqual(pres[0].lines, ["cd repo", "git push"])
        # Following sibling bullet still renders after the block.
        self.assertIn("Check status.", flowables[-1].text)


if __name__ == "__main__":
    unittest.main()
