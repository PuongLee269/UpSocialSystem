"""Test Art Director + route thumbnail AI (imagegen/Ollama giả, không gọi mạng)."""
import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ytagent import art_director, routes  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

VIDEO_ID = "abcd1234abcd1234"
GOOD_JSON = ('Đây:\n{"concepts":[{"style_name":"A","prompt":"warm cinematic","mode":"inpaint_bg","denoise":5,'
             '"text_overlay":"Chuyện lạ","font_preset":"Impact"},{"prompt":""},"x",'
             '{"prompt":"b"},{"prompt":"c"},{"prompt":"d"}]}')


class ConceptTests(unittest.TestCase):
    JOB = {"content": {"title": "Tiêu đề", "tags": ["a"], "description": "d"}, "source": {"name": "clip"}}

    def test_parse_limits_and_cleans(self):
        out = art_director.parse_concepts(GOOD_JSON)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["mode"], "inpaint_bg")
        self.assertEqual(out[0]["denoise"], 1.0)
        self.assertIn("no text", out[0]["prompt"])
        self.assertEqual(out[1]["mode"], "img2img")

    def test_parse_garbage(self):
        for bad in (None, "", "no json", '{"concepts": 5}', "{broken"):
            self.assertEqual(art_director.parse_concepts(bad), [])

    def test_no_llm_uses_template_and_says_so(self):
        res = art_director.make_concepts(self.JOB, "brief")
        self.assertEqual(res["source"], "template")
        self.assertEqual(len(res["concepts"]), 3)
        self.assertIn("không phải AI", res["note"])

    def test_llm_called_once(self):
        calls = []
        res = art_director.make_concepts(self.JOB, "b", lambda p: (calls.append(p), (GOOD_JSON, "Ollama local (m)"))[1])
        self.assertEqual((res["source"], len(calls)), ("ollama", 1))

    def test_llm_failure_falls_back(self):
        for gen in (lambda p: (None, "Ollama chưa sẵn sàng"), lambda p: ("nonsense", "x"), lambda p: 1 / 0):
            res = art_director.make_concepts(self.JOB, "b", gen)
            self.assertEqual(res["source"], "template")


class FakeImageGenError(Exception):
    pass


class FakeImageGen:
    ImageGenError = FakeImageGenError

    def __init__(self, configured=True, fail=False):
        self.configured, self.fail, self.calls = configured, fail, []

    def status(self, cfg):
        return {"configured": self.configured, "reachable": self.configured, "reason": "fake"}

    def list_workflows(self, d):
        return ["wf1"]

    def generate(self, cfg, workflow, frame_file, prompt, negative, mode, denoise, seed, mask_file, out_dir):
        self.calls.append((workflow, prompt, mode, denoise, seed))
        if self.fail:
            raise FakeImageGenError("boom")
        out = Path(out_dir) / "gen1.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), (200, 50, 50)).save(out)
        return {"file": str(out), "seed": seed if seed is not None else 777, "workflow": workflow,
                "prompt": prompt, "ai_generated": True, "warnings": []}


@unittest.skipIf(Image is None, "cần Pillow")
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "videos").mkdir()
        (base / "videos" / "clip.mp4").write_bytes(b"x")
        video = {"id": VIDEO_ID, "name": "clip", "duration": 5, "path": str(base / "videos" / "clip.mp4")}
        self.base = base
        self.make_api(FakeImageGen())
        self.video = video
        self.job = self.api.handle("POST", "/api/jobs", {}, {"video_id": VIDEO_ID})[1]
        frames = base / "outbox" / "jobs" / self.job["id"] / "frames"
        frames.mkdir(parents=True)
        Image.new("RGB", (1280, 720), (30, 90, 160)).save(frames / "frame-01.jpg")
        self.call("PUT", f"/api/jobs/{self.job['id']}/frame",
                  {"expected_revision": self.job["revision"], "data": {"selected_index": 1, "time_sec": 1,
                                                                        "file": "frames/frame-01.jpg"}})

    def make_api(self, ig, gen=None):
        video = {"id": VIDEO_ID, "name": "clip", "duration": 5, "path": str(self.base / "videos" / "clip.mp4")}
        (self.base / "outbox").mkdir(exist_ok=True)
        self.ig = ig
        self.api = routes.Api(self.base / "outbox", lambda: [self.base / "videos"],
                              lambda i: video if i == VIDEO_ID else None,
                              yt_status=lambda: {"configured": False, "authorized": False},
                              ollama_generate=gen, imagegen_cfg=lambda: {}, imagegen=ig)

    def call(self, method, path, body=None):
        return self.api.handle(method, path, {}, body)

    def url(self, tail):
        return f"/api/jobs/{self.job['id']}/{tail}"

    def test_status(self):
        status, body = self.call("GET", "/api/imagegen/status")
        self.assertEqual((status, body["configured"], body["workflows"]), (200, True, ["wf1"]))

    def test_concepts_template_and_persist(self):
        status, body = self.call("POST", self.url("thumbnail/ai-concepts"), {"brief": "ý tưởng"})
        self.assertEqual((status, body["source"]), (200, "template"))
        state = self.call("GET", self.url("thumbnail/ai"))[1]
        self.assertEqual(len(state["concepts"]), 3)
        self.assertEqual(self.call("POST", self.url("thumbnail/ai-concepts"), {"brief": " "})[0], 400)

    def test_generate_creates_ai_version_not_approved(self):
        self.call("POST", self.url("thumbnail/ai-concepts"), {"brief": "x"})
        status, body = self.call("POST", self.url("thumbnail/ai-generate"), {"concept_index": 1, "seed": 5,
                                                                              "workflow": "wf1"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ai_generated"])
        self.assertEqual(body["seed"], 5)
        self.assertTrue(any("không đổi mặt" in w for w in body["warnings"]))
        job = self.call("GET", f"/api/jobs/{self.job['id']}")[1]
        ver = job["thumbnail"]["versions"][-1]
        self.assertTrue(ver["ai_generated"] and ver["prompt"] and ver["seed"] == 5)
        self.assertIsNone(job["thumbnail"]["approved_version"])
        self.assertIn("ai_thumbnail", job["checklist"]["ai_flags"])
        self.assertTrue((self.base / "outbox" / "jobs" / job["id"] / ver["file"]).is_file())

    def test_not_configured_returns_409(self):
        self.make_api(FakeImageGen(configured=False))
        status, body = self.call("POST", self.url("thumbnail/ai-generate"), {"prompt": "p"})
        self.assertEqual((status, body["code"]), (409, "not_configured"))
        self.assertEqual(self.ig.calls, [])

    def test_missing_prompt_and_bad_args(self):
        self.assertEqual(self.call("POST", self.url("thumbnail/ai-generate"), {})[0], 400)
        self.assertEqual(self.call("POST", self.url("thumbnail/ai-generate"), {"concept_index": 9})[0], 400)
        self.assertEqual(self.call("POST", self.url("thumbnail/ai-generate"), {"prompt": "p", "mode": "x"})[0], 400)
        self.assertEqual(self.call("POST", self.url("thumbnail/ai-generate"), {"prompt": "p", "seed": -1})[0], 400)

    def test_generate_failure_is_502_without_version(self):
        self.make_api(FakeImageGen(fail=True))
        status, body = self.call("POST", self.url("thumbnail/ai-generate"), {"prompt": "p"})
        self.assertEqual((status, body["code"]), (502, "imagegen_failed"))
        self.assertEqual(self.call("GET", f"/api/jobs/{self.job['id']}")[1]["thumbnail"]["versions"], [])

    def test_style_reference_roundtrip_and_invalid(self):
        buf = io.BytesIO()
        Image.new("RGB", (50, 50), (1, 2, 3)).save(buf, "PNG")
        data = base64.b64encode(buf.getvalue()).decode()
        status, body = self.call("POST", self.url("thumbnail/style-reference"), {"data_base64": data})
        self.assertEqual((status, body["present"], body["used_by_workflow"]), (200, True, False))
        bad = base64.b64encode(b"not an image").decode()
        self.assertEqual(self.call("POST", self.url("thumbnail/style-reference"), {"data_base64": bad})[0], 400)
        self.assertFalse(self.call("POST", self.url("thumbnail/style-reference"), {"clear": True})[1]["present"])


if __name__ == "__main__":
    unittest.main()
