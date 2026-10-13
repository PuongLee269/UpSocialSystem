"""Test API qua ThreadingHTTPServer thật trên port ngẫu nhiên; YouTube dùng client giả (không gọi mạng)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
from ytagent import routes  # noqa: E402
from ytagent import youtube as yt  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

VIDEO_ID = "abcd1234abcd1234"


class FakeClient:
    def __init__(self):
        self.inserts, self.thumbs, self.publics = 0, 0, 0
        self.fail_with = None

    def insert_private(self, video_path, snippet, made_for_kids=False, on_created=None):
        self.inserts += 1
        self.snippet, self.kids = snippet, made_for_kids
        if self.fail_with:
            raise self.fail_with
        if on_created:
            on_created("VID123")
        return "VID123"

    def set_thumbnail(self, video_id, thumb_path):
        self.thumbs += 1

    def set_public(self, video_id):
        self.publics += 1


class RoutesBase(unittest.TestCase):
    configured = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.outbox, self.root = base / "outbox", base / "videos"
        self.outbox.mkdir()
        self.root.mkdir()
        (self.root / "clip.mp4").write_bytes(b"not a real video")
        self.client = FakeClient()
        video = {"id": VIDEO_ID, "name": "clip", "duration": 5, "path": str(self.root / "clip.mp4")}
        state = {"configured": self.configured, "authorized": self.configured, "reason": "test"}
        self.api = routes.Api(self.outbox, lambda: [self.root], lambda i: video if i == VIDEO_ID else None,
                              client=self.client, yt_status=lambda: state)
        patches = [mock.patch.object(app, "OUTBOX", self.outbox.resolve()), mock.patch.object(app, "API", self.api),
                   mock.patch.object(app.Handler, "log_message", lambda *a, **k: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def req(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, payload

    def new_job(self):
        status, job = self.req("POST", "/api/jobs", {"video_id": VIDEO_ID})
        self.assertEqual(status, 201)
        return job

    def put(self, job_id, section, data):
        _, job = self.req("GET", f"/api/jobs/{job_id}")
        status, out = self.req("PUT", f"/api/jobs/{job_id}/{section}", {"expected_revision": job["revision"], "data": data})
        self.assertEqual(status, 200, out)
        return out

    def ready_job(self):
        """Job đủ mọi gate upload (thumbnail tạo trực tiếp trong store, không cần Pillow/cv2)."""
        job = self.new_job()
        jid = job["id"]
        self.put(jid, "content", {"title": "Tiêu đề", "description": "Mô tả", "tags": ["a", "b"]})
        self.put(jid, "frame", {"selected_index": 1, "time_sec": 1.0, "file": "frames/frame-01.jpg",
                                "crop169": {"x": 0, "y": 0.1, "w": 1, "h": 0.8, "pos": 0.5}})
        thumb = self.outbox / "jobs" / jid / "thumbs"
        thumb.mkdir(parents=True)
        (thumb / "v1.jpg").write_bytes(b"jpg")
        self.api.store.update(jid, lambda j: {**j, "thumbnail": {**j["thumbnail"], "versions": [
            {"v": 1, "file": "thumbs/v1.jpg", "template": "", "text": "", "created": "x", "note": ""}]}})
        status, out = self.req("POST", f"/api/jobs/{jid}/thumbnail/approve", {"version": 1})
        self.assertEqual(status, 200, out)
        self.put(jid, "vidiq", {"confirmed_by_user": True})
        out = self.put(jid, "checklist", {"rights": True, "policy": True, "metadata": True, "disclosure": True,
                                          "kids": True, "ai_flags": ["ai_content"]})
        return out


class JobFlowTests(RoutesBase):
    def test_create_job_idempotent_and_unknown_video(self):
        job = self.new_job()
        self.assertEqual(job["status"], "source_selected")
        self.assertEqual(job["source"]["rel_path"], "0/clip.mp4")
        status, again = self.req("POST", "/api/jobs", {"video_id": VIDEO_ID})
        self.assertEqual(again["id"], job["id"])
        self.assertEqual(self.req("POST", "/api/jobs", {"video_id": "nope"})[0], 404)
        self.assertEqual(len(self.req("GET", "/api/jobs")[1]["jobs"]), 1)

    def test_revision_conflict(self):
        job = self.new_job()
        ok = self.req("PUT", f"/api/jobs/{job['id']}/content", {"expected_revision": 1, "data": {"title": "A"}})
        self.assertEqual(ok[0], 200)
        status, body = self.req("PUT", f"/api/jobs/{job['id']}/content", {"expected_revision": 1, "data": {"title": "B"}})
        self.assertEqual((status, body["code"]), (409, "conflict"))

    def test_status_follows_gates(self):
        job = self.new_job()
        self.assertEqual(self.put(job["id"], "content", {"title": "T", "tags": ["x"]})["status"], "content_ready")
        self.assertEqual(self.put(job["id"], "vidiq", {"confirmed_by_user": True})["status"], "vidiq_reviewed")
        self.assertEqual(self.ready_job()["status"], "thumbnail_approved")

    def test_invalid_section_data(self):
        job = self.new_job()
        for section, data in (("checklist", {"ai_flags": ["bogus"]}), ("content", {"made_for_kids": "yes"}),
                              ("frame", {"crop169": {"x": 2, "y": 0, "w": 1, "h": 1}}),
                              ("frame", {"file": "../../x.jpg"})):
            status, body = self.req("PUT", f"/api/jobs/{job['id']}/{section}", {"expected_revision": 1, "data": data})
            self.assertEqual((status, body["code"]), (400, "invalid"), section)

    def test_templates_integrations_brief(self):
        self.assertEqual(self.req("GET", "/api/templates")[1], {"templates": {}})
        self.assertEqual(self.req("PUT", "/api/templates/mau1", {"data": {"text": "Hi", "template": "mau1"}})[0], 200)
        self.assertIn("mau1", self.req("GET", "/api/templates")[1]["templates"])
        integ = self.req("GET", "/api/integrations")[1]
        self.assertEqual(integ["vidiq"], {"connected": False, "mode": "manual"})
        job = self.ready_job()
        brief = self.req("GET", f"/api/vidiq/brief?job={job['id']}")[1]
        self.assertIn("Tiêu đề", brief["text"])
        self.assertEqual(brief["url"], "https://app.vidiq.com/")

    @unittest.skipIf(Image is None, "cần Pillow")
    def test_render_thumbnail_accepts_frontend_crop(self):
        job = self.new_job()
        frames = self.outbox / "jobs" / job["id"] / "frames"
        frames.mkdir(parents=True)
        Image.new("RGB", (1920, 1080), (30, 90, 160)).save(frames / "frame-01.jpg")
        self.put(job["id"], "frame", {"selected_index": 1, "time_sec": 1, "file": "frames/frame-01.jpg",
                                      "crop169": {"x": 0, "y": 0, "w": 1, "h": 1, "pos": 0.5}})
        status, out = self.req("POST", f"/api/jobs/{job['id']}/thumbnail/render",
                               {"frame_index": 1, "template": "", "text": "Xin chào", "note": "n"})
        self.assertEqual((status, out["version"]), (200, 1))
        saved = self.req("GET", f"/api/jobs/{job['id']}")[1]["thumbnail"]["versions"]
        self.assertEqual(saved[0]["note"], "n")
        self.assertEqual(self.req("GET", out["url"])[0], 200)


class UploadTests(RoutesBase):
    def test_upload_gate_fail(self):
        job = self.new_job()
        status, body = self.req("POST", f"/api/jobs/{job['id']}/upload-private", {})
        self.assertEqual((status, body["code"]), (409, "gate_failed"))
        self.assertTrue(body["reasons"])
        self.assertEqual(self.client.inserts, 0)

    def test_upload_idempotent_and_never_public(self):
        job = self.ready_job()
        url = f"/api/jobs/{job['id']}/upload-private"
        s1, j1 = self.req("POST", url, {})
        s2, j2 = self.req("POST", url, {})
        self.assertEqual((s1, s2), (200, 200), j1)
        self.assertEqual(self.client.inserts, 1)
        self.assertEqual(self.client.publics, 0)
        self.assertEqual((j1["status"], j1["upload"]["state"], j1["upload"]["privacy"]), ("private_uploaded", "done", "private"))
        self.assertEqual(j1["upload"]["youtube_video_id"], "VID123")
        self.assertTrue(j1["upload"]["idempotency_key"])
        self.assertEqual(j2["upload"], j1["upload"])
        self.assertEqual(j2["public"]["state"], "locked")

    def test_upload_failure_keeps_status_and_retries(self):
        job = self.ready_job()
        url = f"/api/jobs/{job['id']}/upload-private"
        self.client.fail_with = yt.YouTubeError("Mạng lỗi", True, "network")
        status, body = self.req("POST", url, {})
        self.assertEqual(status, 502)
        current = self.req("GET", f"/api/jobs/{job['id']}")[1]
        self.assertEqual((current["status"], current["upload"]["state"]), ("thumbnail_approved", "failed"))
        self.assertEqual(current["error"]["stage"], "upload")
        self.client.fail_with = None
        status, ok = self.req("POST", url, {})
        self.assertEqual((status, ok["status"]), (200, "private_uploaded"))
        self.assertIsNone(ok["error"])

    def test_go_public_requires_confirmation(self):
        job = self.ready_job()
        jid = job["id"]
        self.assertEqual(self.req("POST", f"/api/jobs/{jid}/go-public", {})[0], 409)  # chưa upload
        self.req("POST", f"/api/jobs/{jid}/upload-private", {})
        url = f"/api/jobs/{jid}/go-public"
        for body in ({}, {"confirm_video_id": "VID123"}, {"confirm_video_id": "VID123", "confirm_text": "cong khai"},
                     {"confirm_video_id": "OTHER", "confirm_text": "CONG KHAI"}):
            status, out = self.req("POST", url, body)
            self.assertEqual((status, out["code"]), (409, "gate_failed"), body)
        self.assertEqual(self.client.publics, 0)
        status, out = self.req("POST", url, {"confirm_video_id": "VID123", "confirm_text": "CONG KHAI"})
        self.assertEqual((status, out["status"], out["public"]["state"]), (200, "public", "done"))
        self.req("POST", url, {"confirm_video_id": "VID123", "confirm_text": "CONG KHAI"})
        self.assertEqual(self.client.publics, 1)


class NotConfiguredTests(RoutesBase):
    configured = False

    def test_not_configured_is_honest(self):
        job = self.ready_job()
        status, body = self.req("POST", f"/api/jobs/{job['id']}/upload-private", {})
        self.assertEqual((status, body["code"]), (409, "not_configured"))
        self.assertIn("YouTube Studio", body["error"])
        current = self.req("GET", f"/api/jobs/{job['id']}")[1]
        self.assertEqual((current["upload"]["state"], current["status"]), ("none", "thumbnail_approved"))
        self.assertEqual(self.client.inserts, 0)


class SecurityTests(RoutesBase):
    def test_index_and_static(self):
        status, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        self.assertEqual(self.req("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.req("GET", "/static/nope.js")[0], 404)

    def test_static_path_traversal_blocked(self):
        for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%5Capp.py", "/static/%2e%2e%2fconfig.json"):
            status, body = self.req("GET", path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(b"ThreadingHTTPServer", body if isinstance(body, bytes) else b"")

    def test_file_endpoint_boundaries(self):
        job = self.new_job()
        (self.outbox / "secrets").mkdir()
        (self.outbox / "secrets" / "token.json").write_text("{}")
        (self.outbox / "client_secret.json").write_text("{}")
        (self.root / "private.txt").write_text("outside")
        (self.outbox.parent / "outside.txt").write_text("outside")
        for rel in ("../outside.txt", "..%2Foutside.txt", "%2e%2e/videos/clip.mp4", "secrets/token.json",
                    "client_secret.json", "C:/Windows/win.ini", "/etc/passwd", ""):
            status, _ = self.req("GET", "/api/file?path=" + rel)
            self.assertIn(status, (400, 403, 404), rel)
            self.assertNotEqual(status, 200, rel)
        self.assertEqual(self.req("GET", "/api/file?path=secrets/token.json")[0], 403)
        self.assertEqual(self.req("GET", f"/api/file?path=jobs/{job['id']}/job.json")[0], 200)

    def test_post_limits_and_origin(self):
        big = b'{"video_id":"' + b"a" * (300 * 1024) + b'"}'
        self.assertEqual(self.req("POST", "/api/jobs", raw=big)[0], 413)
        self.assertEqual(self.req("POST", "/api/jobs", raw=b"[1]")[0], 400)
        status, _ = self.req("POST", "/api/jobs", {"video_id": VIDEO_ID}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_unknown_route_and_bad_job(self):
        self.assertEqual(self.req("GET", "/api/jobs/zzzzzzzzzz")[0], 404)
        self.assertEqual(self.req("GET", "/api/jobs/../x")[0], 404)


if __name__ == "__main__":
    unittest.main()
