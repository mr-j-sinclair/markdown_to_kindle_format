"""Synthetic tests for the get-n-latest-linkedin-posts Skill's crop_lightbox.py."""

import os
import subprocess
import sys
import tempfile
import unittest

import importlib.util

import numpy as np
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, ".claude", "skills", "get-n-latest-linkedin-posts", "scripts", "crop_lightbox.py")

_spec = importlib.util.spec_from_file_location("crop_lightbox", SCRIPT)
crop_lightbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crop_lightbox)

BACKDROP = (0, 0, 0)
FRAME = (27, 31, 35)  # LinkedIn lightbox frame / side-panel colour observed 2026-10


def _lightbox(photo: np.ndarray, *, photo_xy=(120, 40), size=(1200, 900), frame_x=(60, None)) -> np.ndarray:
    """Dark page backdrop with dimmed text, a frame + side panel with text, and the photo."""
    w, h = size
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = BACKDROP
    img[8:22, 300:700] = (70, 70, 70)  # dimmed page text above the lightbox
    fx0 = frame_x[0]
    img[photo_xy[1]:photo_xy[1] + photo.shape[0], fx0:] = FRAME
    panel_x = photo_xy[0] + photo.shape[1] + 80
    for y in range(photo_xy[1] + 20, photo_xy[1] + 300, 30):  # side-panel text lines
        img[y:y + 10, panel_x:panel_x + 90] = (200, 200, 200)
    x, y = photo_xy
    img[y:y + photo.shape[0], x:x + photo.shape[1]] = photo
    return img


def _dark_photo(w: int, h: int) -> np.ndarray:
    """A mostly dark 'night' photo: values 0..60 with texture and a few bright lights."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 10 + 30 * (xx / w) + 15 * np.sin(yy / 17.0)
    photo = np.stack([base, base * 0.9, base * 1.2], axis=2) + rng.normal(0, 6, (h, w, 3))
    for cx, cy in [(80, 60), (300, 200), (520, 330)]:
        photo[cy - 6:cy + 6, cx - 6:cx + 6] = (240, 220, 160)
    return np.clip(photo, 0, 255).astype(np.uint8)


class CropLightboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, img: np.ndarray, *extra: str):
        src = os.path.join(self.tmp.name, "raw.png")
        out = os.path.join(self.tmp.name, "crop.png")
        Image.fromarray(img).save(src)
        res = subprocess.run([sys.executable, SCRIPT, src, out, *extra], capture_output=True, text=True)
        return res, out

    def test_dark_photo_in_dark_lightbox_survives(self):
        photo = _dark_photo(600, 400)
        res, out = self._run(_lightbox(photo), "--expect", "1200", "800")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("(120, 40, 720, 440)", res.stdout)
        with Image.open(out) as im:
            self.assertEqual(im.size, (600, 400))

    def test_white_graphic_touching_top_left_edge(self):
        # Zoom screenshot cut the photo at the top/left; dark border only bottom/right.
        img = np.zeros((700, 1000, 3), dtype=np.uint8)
        img[:] = FRAME
        img[:600, :800] = 255
        img[100:250, 100:300] = (30, 110, 200)  # logos on a white background
        img[350:500, 450:700] = (10, 10, 60)
        img[50:62, 880:990] = (220, 220, 220)  # side-panel text
        res, out = self._run(img, "--expect", "1600", "1200")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("(0, 0, 800, 600)", res.stdout)

    def test_dark_bottom_photo_filling_frame_height(self):
        # Live 2026-10-09: a photo's black caption band blended into the dark page,
        # but the photo fills the frame height, so the frame's top/bottom are its edges.
        img = np.zeros((1000, 1200, 3), dtype=np.uint8)
        img[20:940, 20:1150] = FRAME
        photo = _dark_photo(736, 920) + 120
        photo[600:] = FRAME                               # bottom band = frame colour
        photo[650:680, 100:600] = 250                     # white caption text in it
        img[20:940, 180:916] = photo
        img[100:112, 1000:1120] = (220, 220, 220)         # side-panel text
        res, out = self._run(img, "--expect", "1080", "1350")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("(180, 20, 916, 940)", res.stdout)

    def test_aspect_mismatch_exits_2_and_still_writes(self):
        res, out = self._run(_lightbox(_dark_photo(600, 400)), "--expect", "1000", "1000")
        self.assertEqual(res.returncode, 2)
        self.assertIn("ASPECT MISMATCH", res.stderr)
        self.assertTrue(os.path.exists(out))

    def test_refuses_to_overwrite_input(self):
        src = os.path.join(self.tmp.name, "raw.png")
        Image.fromarray(_lightbox(_dark_photo(600, 400))).save(src)
        before = os.path.getmtime(src)
        res = subprocess.run([sys.executable, SCRIPT, src, src], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(os.path.getmtime(src), before)

    def test_no_border_exits_1(self):
        noise = np.random.default_rng(3).integers(0, 256, (300, 400, 3), dtype=np.uint8)
        res, out = self._run(noise, "--expect", "400", "300")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no lightbox border found", res.stderr)
        self.assertFalse(os.path.exists(out))

    def test_invalid_arguments_exit_1(self):
        img = _lightbox(_dark_photo(600, 400))
        for extra in (["--expect", "0", "800"], ["--expect", "1200", "-5"],
                      ["--tolerance", "nan"], ["--tolerance", "-0.1"], ["--tolerance", "inf"]):
            with self.subTest(extra=extra):
                res, out = self._run(img, *extra)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn("error:", res.stderr)
                self.assertFalse(os.path.exists(out))

    def test_refuses_alias_of_input(self):
        src = os.path.join(self.tmp.name, "raw.png")
        Image.fromarray(_lightbox(_dark_photo(600, 400))).save(src)
        with open(src, "rb") as f:
            original = f.read()
        sym = os.path.join(self.tmp.name, "sym.png")
        hard = os.path.join(self.tmp.name, "hard.png")
        os.symlink(src, sym)
        os.link(src, hard)
        for alias in (sym, hard):
            with self.subTest(alias=os.path.basename(alias)):
                res = subprocess.run([sys.executable, SCRIPT, src, alias], capture_output=True, text=True)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                with open(src, "rb") as f:
                    self.assertEqual(f.read(), original)



class ChooseExpectedTests(unittest.TestCase):
    def test_closest_aspect_beats_larger_box_with_dark_strip(self):
        # Live 2026-10-09: a 19 px frame strip survived on the larger candidate.
        boxes = [(0, 20, 1208, 812), (19, 20, 1208, 812)]  # aspects 1.525, 1.501
        self.assertEqual(crop_lightbox.choose_expected(boxes, 1.5, 0.03), (19, 20, 1208, 812))

    def test_small_subregion_cannot_win_by_chance(self):
        boxes = [(0, 0, 1208, 873), (4, 4, 590, 422)]  # full photo 0.5% off; one logo 1.8% off
        self.assertEqual(crop_lightbox.choose_expected(boxes, 1460 / 1060, 0.03), (0, 0, 1208, 873))
        boxes = [(0, 0, 1208, 873), (100, 100, 238, 200)]  # tiny box with exact aspect
        self.assertEqual(crop_lightbox.choose_expected(boxes, 1.38, 0.03), (0, 0, 1208, 873))

    def test_lone_tiny_fragment_in_tolerance_does_not_win(self):
        # Codex regression: only the fragment is near the expected aspect.
        for frag in [(100, 100, 238, 200), (100, 100, 250, 200)]:  # 1.38 and exactly 1.5
            with self.subTest(frag=frag):
                boxes = [(0, 0, 1200, 870), frag]
                self.assertEqual(crop_lightbox.choose_expected(boxes, 1.5, 0.03), (0, 0, 1200, 870))

    def test_tie_prefers_larger_and_falls_back_to_closest(self):
        self.assertEqual(crop_lightbox.choose_expected([(0, 0, 100, 100), (0, 0, 200, 200)], 1.0, 0.03),
                         (0, 0, 200, 200))
        self.assertEqual(crop_lightbox.choose_expected([(0, 0, 300, 100), (0, 0, 200, 100)], 1.0, 0.03),
                         (0, 0, 200, 100))


if __name__ == "__main__":
    unittest.main()
