import datetime
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

import ntiy_feed as nf

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ntiy_feed_sample.xml")
BOILERPLATE_LABELS = ("Podcast Homepage", "Bible Reading Plan", "Contact Sean", "on Facebook",
                      "NTIY Website", "Episode Notes")
BOILERPLATE_HOSTS = ("facebook.com", "navigators.org", "mailto:", "newtestamentinayear.com")


def load_fixture():
    with open(FIXTURE, "rb") as f:
        return nf.parse_feed(f.read())


def episode(episodes, prefix):
    return next(e for e in episodes if e.title.startswith(prefix))


def body_of(doc):
    return BeautifulSoup(doc, "html.parser").body


def norm(text):
    return " ".join(text.split())


def source_blocks_after_label(content_html, label_prefix):
    """Top-level, non-empty <p> blocks following a section label in the raw
    RSS HTML -- the episode's narrative body."""
    soup = BeautifulSoup(content_html, "html.parser")
    blocks, seen_label = [], False
    for node in soup.contents:
        text = norm(node.get_text() if isinstance(node, Tag) else str(node))
        if not seen_label:
            seen_label = text.startswith(label_prefix)
            continue
        if isinstance(node, Tag) and node.name == "p" and text:
            blocks.append(node)
    return blocks


class ParseFeedTests(unittest.TestCase):
    def setUp(self):
        self.channel, self.episodes = load_fixture()

    def test_channel_info(self):
        self.assertEqual(self.channel["title"], "Read the Bible: The New Testament in a Year")
        self.assertEqual(self.channel["author"], "Sean Bailey")

    def test_episodes_sorted_oldest_first(self):
        dates = [e.pub_date for e in self.episodes]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(self.episodes[0].title.startswith("Revelation 22"))
        self.assertTrue(self.episodes[-1].title.startswith("1 John 5. Day 198"))

    def test_same_day_release_keeps_both_in_order(self):
        titles = [e.title for e in self.episodes]
        day197 = next(i for i, t in enumerate(titles) if "Day 197" in t)
        day198 = next(i for i, t in enumerate(titles) if "Day 198" in t)
        self.assertEqual(day198, day197 + 1)

    def test_guids_unique(self):
        guids = [e.guid for e in self.episodes]
        self.assertEqual(len(guids), len(set(guids)))


class NamingTests(unittest.TestCase):
    def names(self, title):
        return nf.episode_names(nf.Episode("g", title, "", None, ""))

    def test_current_title_format(self):
        self.assertEqual(self.names("1 John 5. Day 198 - Read the Bible: The New Testament in a Year"),
                         ("ntiy-day-198-1-john-5", "1 John 5 — Day 198"))

    def test_previous_cycle_title_format(self):
        self.assertEqual(self.names("Revelation 12 -  Day 250 - The New Testament in a Year"),
                         ("ntiy-day-250-revelation-12", "Revelation 12 — Day 250"))

    def test_prefixed_and_unspaced_titles(self):
        self.assertEqual(self.names("NEW!  Hebrews 6. Day 50 - Read the Bible: The New Testament in a Year")[0],
                         "ntiy-day-050-hebrews-6")
        self.assertEqual(self.names("Hebrews 12. Day 56 -Read the Bible: The New Testament in a Year")[0],
                         "ntiy-day-056-hebrews-12")

    def test_unnumbered_title_falls_back_to_slug(self):
        slug, title = self.names("Bonus Episode – Eighteen Inches from Heaven featuring Brock Baber")
        self.assertTrue(slug.startswith("ntiy-bonus-episode-eighteen-inches"))
        self.assertFalse(slug.endswith("-"))
        self.assertEqual(title, "Bonus Episode – Eighteen Inches from Heaven featuring Brock Baber")


class BuildDocumentTests(unittest.TestCase):
    def setUp(self):
        self.channel, self.episodes = load_fixture()

    def build(self, prefix):
        ep = episode(self.episodes, prefix)
        return ep, body_of(nf.build_document(ep)[2])

    def test_boilerplate_dropped_from_every_fixture_episode(self):
        for ep in self.episodes:
            with self.subTest(ep.title):
                body = body_of(nf.build_document(ep)[2])
                text = body.get_text(" ")
                for label in BOILERPLATE_LABELS:
                    self.assertNotIn(label, text)
                for a in body.find_all("a"):
                    if a["href"] == ep.link:
                        continue
                    self.assertFalse(any(h in a["href"] for h in BOILERPLATE_HOSTS), a["href"])

    def test_heading_and_episode_page_link(self):
        for ep in self.episodes:
            with self.subTest(ep.title):
                body = body_of(nf.build_document(ep)[2])
                self.assertEqual(body.find("h1").get_text(), nf.episode_names(ep)[1])
                self.assertIsNotNone(body.find("a", href=ep.link))

    def test_scripture_link_kept(self):
        for prefix in ("1 John 5", "1 John 3", "Titus 2", "James 5"):
            with self.subTest(prefix):
                _, body = self.build(prefix)
                link = body.find("a", href=lambda h: h and "biblegateway.com" in h)
                self.assertIsNotNone(link)
                self.assertIn("Today's Scripture:", link.parent.get_text())

    def test_title_line_not_repeated(self):
        for ep in self.episodes:
            with self.subTest(ep.title):
                body = body_of(nf.build_document(ep)[2])
                blocks = body.find_all(recursive=False)
                self.assertNotIn(ep.title, norm(" ".join(b.get_text() for b in blocks[1:-1])))

    def test_bare_section_marker_becomes_heading(self):
        for prefix in ("1 John 5", "1 John 3", "Titus 2"):
            with self.subTest(prefix):
                _, body = self.build(prefix)
                self.assertEqual([h.get_text() for h in body.find_all("h2")], ["In Today's Episode"])

    def test_inline_boilerplate_truncated_after_scripture_link(self):
        _, body = self.build("Titus 2")
        line = body.find("a", href=lambda h: h and "biblegateway.com" in h).parent
        self.assertEqual(norm(line.get_text()), "Today's Scripture: Titus 2 (CSB)")

    def test_empty_notes_still_produce_document(self):
        _, body = self.build("Revelation 22")
        self.assertEqual([b.name for b in body.find_all(recursive=False)], ["h1", "img", "p"])

    def test_cover_art_follows_heading(self):
        for ep in self.episodes:
            with self.subTest(ep.title):
                body = body_of(nf.build_document(ep)[2])
                img = body.find("h1").find_next_sibling()
                self.assertEqual(img.name, "img")
                self.assertTrue(img["src"].startswith("https://pbcdn1.podbean.com/"))
                self.assertEqual(img["src"], ep.image_url)

    def test_key_verse_wrapped_in_blockquote(self):
        expected = {
            "1 John 5": "\"And this is the testimony: God has given us eternal life, and this life is in his Son.\" — 1 John 5:11",
            "1 John 3": "— 1 John 3:1",
            "Titus 2": "— Titus 2:11–12",
            "Luke 4": "\"It is written...\" Luke 4:4",
            "James 5": "James 5:16",
            "Bonus Episode": "Hebrews 3:15",
        }
        for prefix, tail in expected.items():
            with self.subTest(prefix):
                _, body = self.build(prefix)
                quotes = body.find_all("blockquote")
                self.assertEqual(len(quotes), 1)
                self.assertTrue(norm(quotes[0].get_text()).endswith(tail))
                self.assertEqual([c.name for c in quotes[0].find_all(recursive=False)], ["p"])

    def test_key_verse_emphasis_kept_inside_blockquote(self):
        _, body = self.build("1 John 5")
        em = body.find("blockquote").find("em")
        self.assertEqual(norm(em.get_text()),
                         "\"And this is the testimony: God has given us eternal life, and this life is in his Son.\"")

    def test_body_paragraphs_not_blockquoted(self):
        _, body = self.build("1 John 5")
        for p in body.find_all("p"):
            if norm(p.get_text()).startswith("First John 5 closes"):
                self.assertIsNone(p.find_parent("blockquote"))

    def assert_semantic_fidelity(self, prefix, label):
        ep, body = self.build(prefix)
        source = source_blocks_after_label(ep.content_html, label)
        self.assertGreater(len(source), 0)
        heading = next(h for h in body.find_all("h2") if h.get_text().startswith(label))
        output = []
        for b in heading.find_next_siblings():
            output += [b] if b.name == "p" else b.find_all("p") if b.name == "blockquote" else []
        output = output[:len(source)]
        self.assertEqual(len(output), len(source))
        for src, out in zip(source, output):
            # Same visible text, same paragraph order, emphasis retained.
            self.assertEqual(norm(out.get_text()), norm(src.get_text()))
            self.assertEqual([norm(e.get_text()) for e in out.find_all("em")],
                             [norm(e.get_text()) for e in src.find_all("em")])

    def test_semantic_fidelity_current_format(self):
        self.assert_semantic_fidelity("1 John 5", "In Today")

    def test_semantic_fidelity_bare_text_format(self):
        self.assert_semantic_fidelity("1 John 3", "In Today")

    def test_semantic_fidelity_highlights_format(self):
        self.assert_semantic_fidelity("Luke 4", "Episode Highlights")
        self.assert_semantic_fidelity("James 5", "Episode Highlights")

    def test_bonus_intro_kept_before_highlights(self):
        _, body = self.build("Bonus Episode")
        intro = body.find_all("p")[0].get_text()
        self.assertIn("special bonus message from worship leader, Brock Baber.", intro)
        self.assert_semantic_fidelity("Bonus Episode", "Episode Highlights")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.channel, self.episodes = load_fixture()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state_path = os.path.join(self.tmp, "state", "ntiy_state.json")
        for name, value in (("INPUT_DIR", os.path.join(self.tmp, "inputs")),
                            ("prepare_cover", lambda episode, slug: None)):
            patcher = patch.object(nf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, leave_newest):
        state = nf.load_state(self.state_path)
        nf.cmd_seed(self.episodes, state, self.state_path, leave_newest)

    def next(self, converter_rc=0, max_sends=5, dry_run=False):
        state = nf.load_state(self.state_path)
        with patch.object(nf, "run_converter", return_value=converter_rc) as conv:
            rc = nf.cmd_next(self.episodes, self.channel, state, self.state_path, max_sends, dry_run)
        return rc, conv

    def processed(self):
        return nf.load_state(self.state_path)["processed"]

    def test_seed_marks_everything_without_sending(self):
        self.seed(0)
        self.assertEqual(set(self.processed()), {e.guid for e in self.episodes})
        rc, conv = self.next()
        self.assertEqual(rc, nf.EXIT_NOTHING_NEW)
        conv.assert_not_called()

    def test_two_unseen_processed_oldest_first_one_per_call(self):
        self.seed(2)
        rc, conv = self.next()
        self.assertEqual(rc, nf.EXIT_DELIVERED)
        self.assertIn("Day 197", conv.call_args.args[2])
        self.assertIn(self.episodes[-2].guid, self.processed())
        self.assertNotIn(self.episodes[-1].guid, self.processed())

        rc, conv = self.next()
        self.assertEqual(rc, nf.EXIT_DELIVERED)
        self.assertIn("Day 198", conv.call_args.args[2])
        self.assertEqual(self.processed()[self.episodes[-1].guid]["how"], "sent")

        rc, _ = self.next()
        self.assertEqual(rc, nf.EXIT_NOTHING_NEW)

    def test_conversion_or_send_failure_leaves_episode_unseen(self):
        self.seed(1)
        for converter_rc in (1, 2):
            with self.subTest(converter_rc=converter_rc):
                rc, _ = self.next(converter_rc=converter_rc)
                self.assertEqual(rc, converter_rc)
                self.assertNotIn(self.episodes[-1].guid, self.processed())
        rc, _ = self.next()
        self.assertEqual(rc, nf.EXIT_DELIVERED)
        self.assertIn(self.episodes[-1].guid, self.processed())

    def test_flood_guard_sends_nothing(self):
        rc, conv = self.next(max_sends=5)  # empty state: all 8 fixture items unseen
        self.assertEqual(rc, nf.EXIT_FLOOD_GUARD)
        conv.assert_not_called()
        self.assertEqual(self.processed(), {})

    def test_dry_run_writes_input_only(self):
        self.seed(1)
        rc, conv = self.next(dry_run=True)
        self.assertEqual(rc, nf.EXIT_DELIVERED)
        conv.assert_not_called()
        self.assertNotIn(self.episodes[-1].guid, self.processed())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "inputs", "ntiy-day-198-1-john-5.html")))

    def test_state_round_trip(self):
        state = {"processed": {"a": {"title": "A", "how": "sent", "at": "x"}}}
        nf.save_state(self.state_path, state)
        self.assertEqual(nf.load_state(self.state_path), state)
        self.assertEqual(os.listdir(os.path.dirname(self.state_path)), ["ntiy_state.json"])


class CoverBannerTests(unittest.TestCase):
    def square_art(self, colour=(200, 30, 30)):
        buf = io.BytesIO()
        Image.new("RGB", (1563, 1563), colour).save(buf, "PNG")
        return buf.getvalue()

    def test_banner_is_banner_sized_jpeg_with_art_centred(self):
        banner = Image.open(io.BytesIO(nf.make_banner(self.square_art())))
        self.assertEqual(banner.format, "JPEG")
        self.assertEqual(banner.size, nf.BANNER_SIZE)
        centre = banner.getpixel((600, 314))
        self.assertTrue(all(abs(a - b) < 8 for a, b in zip(centre, (200, 30, 30))))
        # Side fill is the blurred, darkened art, not the art itself.
        self.assertLess(sum(banner.getpixel((20, 314))), sum(centre))

    def test_prepare_cover_writes_local_banner(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        ep = nf.Episode("g", "t", "", None, "", image_url="https://example.com/a.png")
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = self.square_art()
        with patch.object(nf, "INPUT_DIR", tmp), patch.object(nf.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(nf.prepare_cover(ep, "ntiy-day-198-1-john-5"), "ntiy-day-198-1-john-5-cover.jpg")
        self.assertEqual(Image.open(os.path.join(tmp, "ntiy-day-198-1-john-5-cover.jpg")).size, nf.BANNER_SIZE)

    def test_prepare_cover_failure_falls_back_to_remote_url(self):
        ep = nf.Episode("g", "1 John 5. Day 198 - x", "https://x/e", datetime.datetime(2026, 10, 6), "",
                        image_url="https://example.com/a.png")
        with patch.object(nf.urllib.request, "urlopen", side_effect=OSError("offline")):
            src = nf.prepare_cover(ep, "slug")
        self.assertIsNone(src)
        img = body_of(nf.build_document(ep, src)[2]).find("img")
        self.assertEqual(img["src"], "https://example.com/a.png")

    def test_local_banner_used_when_given(self):
        ep = nf.Episode("g", "1 John 5. Day 198 - x", "https://x/e", datetime.datetime(2026, 10, 6), "",
                        image_url="https://example.com/a.png")
        img = body_of(nf.build_document(ep, "slug-cover.jpg")[2]).find("img")
        self.assertEqual(img["src"], "slug-cover.jpg")


class RunConverterTests(unittest.TestCase):
    def run_with(self, send):
        with patch.object(nf.subprocess, "run") as run:
            run.return_value.returncode = 0
            nf.run_converter("inputs/x.html", "x", "1 John 5 — Day 198",
                             {"title": "Show", "author": "Sean Bailey"}, send=send)
        return run.call_args.args[0]

    def test_send_uses_cli_default(self):
        cmd = self.run_with(send=True)
        self.assertEqual(cmd[1], nf.CONVERTER)
        self.assertTrue(cmd[3].endswith(os.path.join("outputs", "x.epub")))
        self.assertIn("1 John 5 — Day 198", cmd)
        self.assertIn("Sean Bailey", cmd)
        self.assertNotIn("--no-send-to-kindle", cmd)

    def test_preview_suppresses_send(self):
        self.assertIn("--no-send-to-kindle", self.run_with(send=False))


if __name__ == "__main__":
    unittest.main()
