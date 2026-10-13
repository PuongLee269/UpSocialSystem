import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytagent import thumbnail as T  # noqa: E402

try:
    import cv2
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover
    cv2 = None


def make_frame(path, size=(640, 360), color=(30, 120, 200)):
    img = Image.new("RGB", size, color)
    for x in range(0, size[0], 40):  # texture so JPEG isn't trivial
        for y in range(0, size[1], 40):
            img.putpixel((x, y), (255, 255, 0))
    img.save(path, "JPEG")
    return path


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_vietnamese_text_1280x720_under_2mb(self):
        frame = make_frame(self.dir / "f.jpg")
        res = T.render_thumbnail(frame, self.dir / "out" / "v1.jpg", "Bí mật Đà Lạt ở đây!", {"box": True})
        with Image.open(res["file"]) as im:
            self.assertEqual(im.size, (1280, 720))
            self.assertEqual(im.format, "JPEG")
        self.assertLess(res["bytes"], 2 * 1024 * 1024)
        self.assertIsNotNone(res["text_box"])

    def test_text_changes_pixels_but_no_text_keeps_frame(self):
        frame = make_frame(self.dir / "f.jpg", size=(1280, 720))
        a = T.render_thumbnail(frame, self.dir / "a.jpg", "")
        b = T.render_thumbnail(frame, self.dir / "b.jpg", "Xin chào")
        self.assertIsNone(a["text_box"])
        self.assertNotEqual((self.dir / "a.jpg").read_bytes(), (self.dir / "b.jpg").read_bytes())

    def test_text_stays_in_safe_area_and_off_ui_corner(self):
        frame = make_frame(self.dir / "f.jpg")
        res = T.render_thumbnail(frame, self.dir / "o.jpg", "Chữ rất dài để kiểm tra vùng an toàn",
                                 {"position": "bottom-right", "font_size": 80})
        x0, y0, x1, y1 = res["text_box"]
        self.assertGreaterEqual(x0, 1280 * 0.05 - 1)
        self.assertLessEqual(x1, 1280 * 0.95 + 1)
        self.assertGreaterEqual(y0, 720 * 0.05 - 1)
        self.assertLessEqual(y1, 720 * 0.95 + 1)
        self.assertFalse(x1 > 1280 * 0.80 and y1 > 720 * 0.86 and "timestamp" not in " ".join(res["warnings"]))

    def test_crop169_region(self):
        src = self.dir / "tall.jpg"
        Image.new("RGB", (800, 800), (200, 10, 10)).save(src)
        res = T.render_thumbnail(src, self.dir / "o.jpg", "", crop169={"x": 0, "y": 0.25, "w": 1, "h": 0.5625})
        with Image.open(res["file"]) as im:
            self.assertEqual(im.size, (1280, 720))
        with self.assertRaises(T.ThumbnailError):
            T.render_thumbnail(src, self.dir / "o2.jpg", "", crop169={"x": 1})

    def test_invalid_inputs_raise_clear_errors(self):
        with self.assertRaises(T.ThumbnailError):
            T.render_thumbnail(self.dir / "missing.jpg", self.dir / "o.jpg", "x")
        frame = make_frame(self.dir / "f.jpg")
        with self.assertRaises(T.ThumbnailError):
            T.render_thumbnail(frame, self.dir / "o.jpg", "x", {"color": "red"})
        with self.assertRaises(T.ThumbnailError):
            T.render_thumbnail(frame, self.dir / "o.jpg", "x", {"position": "nowhere"})

    def test_mockup_mobile(self):
        frame = make_frame(self.dir / "f.jpg")
        T.render_thumbnail(frame, self.dir / "t.jpg", "Hi")
        out = T.mockup_mobile(self.dir / "t.jpg", self.dir / "m.jpg")
        with Image.open(out) as im:
            self.assertGreaterEqual(im.size[0], 168)
            self.assertGreaterEqual(im.size[1], 94)

    def test_templates_roundtrip_atomic(self):
        path = self.dir / "sub" / "templates.json"
        self.assertEqual(T.load_templates(path), {})
        saved = T.save_template(path, "vang", {"font_size": 80, "color": "#FFD400"})
        self.assertEqual(saved["stroke"], "#000000")
        T.save_template(path, "trang", {"position": "top"})
        data = T.load_templates(path)
        self.assertEqual(set(data), {"vang", "trang"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["vang"]["font_size"], 80)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["templates.json"])
        with self.assertRaises(T.ThumbnailError):
            T.save_template(path, "", {})
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(T.ThumbnailError):
            T.load_templates(path)

    def test_versions_and_history_entry(self):
        d = self.dir / "thumbs"
        self.assertEqual(T.next_version(d), 1)
        d.mkdir()
        (d / "v1.jpg").write_bytes(b"x")
        (d / "v3.jpg").write_bytes(b"x")
        (d / "other.jpg").write_bytes(b"x")
        self.assertEqual(T.next_version(d), 4)
        e = T.history_entry(4, "thumbs/v4.jpg", "vang", "Chào", "ghi chú")
        self.assertEqual(set(e), {"v", "file", "template", "text", "created", "note"})

    def test_find_font_returns_path_or_warning(self):
        path, warn = T.find_font(True)
        self.assertTrue(path is not None or warn)

    @unittest.skipIf(cv2 is None, "cần cv2/numpy")
    def test_contact_sheet_from_fake_video(self):
        video = self.dir / "v.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
        for i in range(60):
            writer.write(np.full((90, 160, 3), (i * 4 % 255, 50, 100), dtype=np.uint8))
        writer.release()
        before = video.read_bytes()
        out = T.contact_sheet(video, self.dir / "sheet", n=6)
        self.assertEqual(len(out), 6)
        self.assertEqual([o["index"] for o in out], list(range(1, 7)))
        self.assertEqual(sorted(o["time_sec"] for o in out), [o["time_sec"] for o in out])
        for o in out:
            self.assertTrue((self.dir / "sheet" / o["file"]).is_file())
            self.assertTrue((self.dir / "sheet" / o["thumb"]).is_file())
        self.assertEqual(video.read_bytes(), before)
        with self.assertRaises(T.ThumbnailError):
            T.contact_sheet(self.dir / "none.avi", self.dir / "s2")


if __name__ == "__main__":
    unittest.main()
