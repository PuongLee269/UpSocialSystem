import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from ytagent import imagegen
from ytagent.imagegen import ImageGenError

WF_DIR = Path(__file__).resolve().parent.parent / "workflows"
GEN_COLOR = (10, 200, 30)


class FakeComfy(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.uploads, self.prompts, self.mode = [], [], "ok"
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/system_stats":
            return self._send(200, {"system": {}})
        if u.path.startswith("/history/"):
            if self.server.mode == "timeout":
                return self._send(200, {})
            if self.server.mode == "exec_error":
                return self._send(200, {"p1": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"node_type": "KSampler", "exception_message": "CUDA OOM"}]]},
                    "outputs": {}}})
            return self._send(200, {"p1": {"status": {"status_str": "success"}, "outputs": {
                "9": {"images": [{"filename": "o.png", "subfolder": "", "type": "output"}]}}}})
        if u.path == "/view":
            assert parse_qs(u.query)["filename"] == ["o.png"]
            buf = io.BytesIO()
            Image.new("RGB", (64, 48), GEN_COLOR).save(buf, "PNG")
            return self._send(200, buf.getvalue(), "image/png")
        self._send(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/upload/image":
            self.server.uploads.append(body)
            return self._send(200, {"name": f"up{len(self.server.uploads)}.png", "subfolder": ""})
        if self.path == "/prompt":
            if self.server.mode == "missing_model":
                return self._send(400, {"error": {"message": "Prompt outputs failed validation"},
                                        "node_errors": {"1": {"class_type": "CheckpointLoaderSimple", "errors": [
                                            {"type": "value_not_in_list", "message": "x",
                                             "extra_info": {"input_name": "ckpt_name", "received_value": "m.safetensors"}}]}}})
            self.server.prompts.append(json.loads(body)["prompt"])
            return self._send(200, {"prompt_id": "p1"})
        self._send(404, {})


class ImageGenTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeComfy()
        self.addCleanup(self.srv.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"comfyui_url": self.srv.url, "comfyui_workflows_dir": str(WF_DIR)}
        self.frame = self.tmp / "frame.jpg"
        Image.new("RGB", (64, 48), (200, 0, 0)).save(self.frame, "JPEG", quality=100)
        self.out = self.tmp / "out"

    def gen(self, **kw):
        args = dict(workflow_name="img2img_sdxl", frame_file=self.frame, prompt="neon city",
                    out_dir=self.out, timeout=3)
        args.update(kw)
        return imagegen.generate(self.cfg, **args)

    # status / list
    def test_status_not_configured(self):
        s = imagegen.status({"comfyui_url": ""})
        self.assertFalse(s["configured"])
        self.assertIn("Chưa cấu hình", s["reason"])

    def test_status_unreachable_and_ok(self):
        s = imagegen.status({"comfyui_url": "http://127.0.0.1:1"})
        self.assertTrue(s["configured"])
        self.assertFalse(s["reachable"])
        self.assertTrue(imagegen.status(self.cfg)["reachable"])

    def test_list_workflows(self):
        names = {w["name"]: w for w in imagegen.list_workflows(WF_DIR)}
        self.assertIn("inpaint_bg_sdxl", names)
        self.assertIn("mask", names["inpaint_bg_sdxl"]["placeholders"])
        self.assertEqual(names["img2img_sdxl"]["model"], "sd_xl_base_1.0.safetensors")

    # generate
    def test_img2img_substitutes_and_writes_sidecar(self):
        r = self.gen(seed=42, denoise=0.4, negative="blur")
        self.assertTrue(r["ai_generated"])
        self.assertEqual(r["seed"], 42)
        self.assertTrue(Path(r["file"]).is_file())
        meta = json.loads(Path(r["meta_file"]).read_text(encoding="utf-8"))
        self.assertEqual((meta["prompt"], meta["workflow"], meta["ai_generated"]), ("neon city", "img2img_sdxl", True))
        self.assertEqual(meta["model"], "sd_xl_base_1.0.safetensors")
        g = self.srv.prompts[0]
        self.assertEqual(g["6"]["inputs"]["seed"], 42)
        self.assertEqual(g["6"]["inputs"]["denoise"], 0.4)
        self.assertEqual(g["4"]["inputs"]["text"], "neon city")
        self.assertEqual(g["5"]["inputs"]["text"], "blur")
        self.assertEqual(g["2"]["inputs"]["image"], "up1.png")

    def test_txt2img_no_upload_uses_size(self):
        r = self.gen(workflow_name="txt2img_sdxl", mode="txt2img", frame_file=None, seed=1)
        self.assertEqual(self.srv.uploads, [])
        self.assertEqual(self.srv.prompts[0]["2"]["inputs"]["width"], 1280)
        self.assertTrue(r["ai_generated"])

    def test_inpaint_bg_keeps_original_pixels(self):
        mask = self.tmp / "mask.png"
        m = Image.new("L", (64, 48), 0)
        m.paste(255, (0, 0, 32, 48))  # nua trai = nguoi (giu nguyen)
        m.save(mask)
        r = self.gen(workflow_name="inpaint_bg_sdxl", mode="inpaint_bg", mask_file=mask, seed=7)
        im = Image.open(r["file"]).convert("RGB")
        self.assertEqual(im.size, (64, 48))
        left, right = im.getpixel((5, 24)), im.getpixel((60, 24))
        self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(left, (200, 0, 0))), left)  # goc (JPEG)
        self.assertEqual(right, GEN_COLOR)  # nen da duoc sinh
        self.assertEqual(len(self.srv.uploads), 2)
        sent_mask = Image.open(io.BytesIO(self.srv.uploads[1][self.srv.uploads[1].index(b"\x89PNG"):])).convert("L")
        self.assertEqual(sent_mask.getpixel((5, 24)), 0)      # mask gui di da dao
        self.assertEqual(sent_mask.getpixel((60, 24)), 255)
        self.assertTrue(json.loads(Path(r["meta_file"]).read_text(encoding="utf-8"))["face_preserved_by_paste"])

    def test_inpaint_requires_mask(self):
        with self.assertRaises(ImageGenError):
            self.gen(workflow_name="inpaint_bg_sdxl", mode="inpaint_bg")

    # errors
    def test_missing_model_message(self):
        self.srv.mode = "missing_model"
        with self.assertRaisesRegex(ImageGenError, "Thiếu model"):
            self.gen()

    def test_timeout(self):
        self.srv.mode = "timeout"
        with self.assertRaisesRegex(ImageGenError, "Quá thời gian"):
            self.gen(timeout=1)

    def test_queue_execution_error(self):
        self.srv.mode = "exec_error"
        with self.assertRaisesRegex(ImageGenError, "CUDA OOM"):
            self.gen()

    def test_bad_workflow_and_names(self):
        bad = self.tmp / "wf"
        bad.mkdir()
        (bad / "ui.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        self.cfg["comfyui_workflows_dir"] = str(bad)
        with self.assertRaisesRegex(ImageGenError, "API Format"):
            self.gen(workflow_name="ui")
        with self.assertRaises(ImageGenError):
            self.gen(workflow_name="../secrets")
        with self.assertRaises(ImageGenError):
            self.gen(workflow_name="missing")

    def test_unconfigured_and_bad_args(self):
        with self.assertRaises(ImageGenError):
            imagegen.generate({"comfyui_url": ""}, "img2img_sdxl", self.frame, "x", out_dir=self.out)
        with self.assertRaises(ImageGenError):
            self.gen(mode="weird")
        with self.assertRaises(ImageGenError):
            self.gen(denoise=1.5)
        with self.assertRaises(ImageGenError):
            self.gen(prompt="  ")

    def test_server_down(self):
        self.cfg["comfyui_url"] = "http://127.0.0.1:1"
        with self.assertRaisesRegex(ImageGenError, "Không kết nối"):
            self.gen()


if __name__ == "__main__":
    unittest.main()
