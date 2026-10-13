import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ytagent import youtube as yt  # noqa: E402


def resp(status=200, headers=None, body=None):
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    return status, {k.lower(): v for k, v in (headers or {}).items()}, raw


class FakeTransport:
    """Scripted transport: handler(method, url, headers, body) -> response."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers or {}), body))
        return self.handler(method, url, headers, body, self)

    def count(self, method=None, needle=""):
        return sum(1 for m, u, _, _ in self.calls
                   if (method is None or m == method) and needle in u)


def make_secrets(tmp, token=True, expires=None):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, yt.CLIENT_SECRET_FILE), "w") as fh:
        json.dump({"installed": {"client_id": "cid", "client_secret": "csec"}}, fh)
    if token:
        yt.save_token({"access_token": "AT", "refresh_token": "RT",
                       "expires_at": expires if expires is not None
                       else time.time() + 3600}, tmp)


def job(key="k1", vid=None, thumb_set=None):
    up = {"idempotency_key": key, "youtube_video_id": vid}
    if thumb_set is not None:
        up["thumbnail_set"] = thumb_set
    return {"content": {"title": "T", "description": "D", "tags": ["a", "b c"]},
            "upload": up}


def happy(method, url, headers, body, t):
    if "uploadType=resumable" in url and method == "POST":
        return resp(200, {"Location": "https://upload.example/s1"})
    if url.startswith("https://upload.example/s1"):
        return resp(200, body={"id": "VID1"})
    if "thumbnails/set" in url:
        return resp(200, body={})
    if method == "GET" and "videos?part=status" in url:
        return resp(200, body={"items": [{"status": {"privacyStatus": "private",
                                                     "selfDeclaredMadeForKids": False}}]})
    if method == "PUT" and "videos?part=status" in url:
        return resp(200, body={"id": "VID1", "status": json.loads(body)["status"]})
    return resp(404, body={})


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = os.path.join(self._td.name, "secrets")
        self.video = os.path.join(self._td.name, "v.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"x" * 1000)
        self.thumb = os.path.join(self._td.name, "t.jpg")
        with open(self.thumb, "wb") as fh:
            fh.write(b"jpg")
        self.sleeps = []

    def client(self, handler=happy, **kw):
        self.t = FakeTransport(handler)
        return yt.YouTubeClient(self.dir, transport=self.t, sleep=self.sleeps.append, **kw)


class StatusTests(Base):
    def test_missing_client_secret_not_configured_vietnamese(self):
        st = yt.status(self.dir)
        self.assertFalse(st["configured"])
        self.assertFalse(st["authorized"])
        self.assertIn("YouTube Studio", st["reason"])

    def test_configured_but_not_authorized(self):
        make_secrets(self.dir, token=False)
        st = yt.status(self.dir)
        self.assertTrue(st["configured"])
        self.assertFalse(st["authorized"])

    def test_authorized_and_no_secret_leak(self):
        make_secrets(self.dir)
        st = yt.status(self.dir)
        self.assertEqual((st["configured"], st["authorized"]), (True, True))
        self.assertNotIn("csec", json.dumps(st))
        self.assertNotIn("RT", json.dumps(st))

    def test_corrupt_client_secret(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, yt.CLIENT_SECRET_FILE), "w") as fh:
            fh.write("{bad")
        self.assertFalse(yt.status(self.dir)["configured"])

    def test_token_file_saved(self):
        make_secrets(self.dir)
        self.assertEqual(yt.load_token(self.dir)["refresh_token"], "RT")
        if os.name != "nt":
            mode = os.stat(os.path.join(self.dir, yt.TOKEN_FILE)).st_mode & 0o777
            self.assertEqual(mode, 0o600)


class OAuthTests(Base):
    def test_loopback_flow_exchanges_code_and_saves_token(self):
        make_secrets(self.dir, token=False)
        seen = {}

        def handler(method, url, headers, body, t):
            seen["body"] = body.decode()
            return resp(200, body={"access_token": "A", "refresh_token": "R",
                                   "expires_in": 3600})

        def fake_browser(auth_url):
            q = dict(p.split("=", 1) for p in auth_url.split("?", 1)[1].split("&"))
            from urllib.parse import unquote
            redirect = unquote(q["redirect_uri"])
            seen["scope"] = unquote(q["scope"])
            seen["challenge"] = q["code_challenge_method"]

            def hit():
                urllib.request.urlopen(redirect + "?code=CODE&state=" + q["state"]).read()
            threading.Thread(target=hit).start()

        tok = yt.authorize(self.dir, open_browser=fake_browser, timeout=10,
                           transport=FakeTransport(handler))
        self.assertEqual(tok["refresh_token"], "R")
        self.assertIn("code=CODE", seen["body"])
        self.assertIn("code_verifier=", seen["body"])
        self.assertEqual(seen["challenge"], "S256")
        self.assertIn("youtube.upload", seen["scope"])
        self.assertTrue(yt.status(self.dir)["authorized"])

    def test_wrong_state_is_ignored_then_times_out(self):
        make_secrets(self.dir, token=False)

        def browser(auth_url):
            from urllib.parse import unquote
            redirect = unquote(auth_url.split("redirect_uri=")[1].split("&")[0])
            threading.Thread(target=lambda: urllib.request.urlopen(
                redirect + "?code=EVIL&state=WRONG").read()).start()

        with self.assertRaises(yt.YouTubeError) as cm:
            yt.authorize(self.dir, open_browser=browser, timeout=2,
                         transport=FakeTransport(lambda *a: resp(500)))
        self.assertEqual(cm.exception.code, "not_authorized")

    def test_expired_token_is_refreshed_and_saved(self):
        make_secrets(self.dir, expires=1)

        def handler(method, url, headers, body, t):
            if "oauth2" in url:
                return resp(200, body={"access_token": "NEW", "expires_in": 3600})
            return happy(method, url, headers, body, t)

        c = self.client(handler)
        c.set_thumbnail("VID1", self.thumb)
        saved = yt.load_token(self.dir)
        self.assertEqual(saved["access_token"], "NEW")
        self.assertEqual(saved["refresh_token"], "RT")

    def test_invalid_grant_requires_reauth(self):
        make_secrets(self.dir, expires=1)
        c = self.client(lambda *a: resp(400, body={"error": "invalid_grant"}))
        with self.assertRaises(yt.YouTubeError) as cm:
            c.set_thumbnail("V", self.thumb)
        self.assertEqual(cm.exception.code, "reauth_required")
        self.assertFalse(cm.exception.retryable)


class ClassifyTests(unittest.TestCase):
    def test_classification(self):
        quota = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode()
        forb = json.dumps({"error": {"errors": [{"reason": "forbidden"}]}}).encode()
        self.assertTrue(yt.classify_error(401).retryable)
        e = yt.classify_error(403, quota)
        self.assertEqual((e.code, e.retryable), ("quota", True))
        self.assertFalse(yt.classify_error(403, forb).retryable)
        self.assertTrue(yt.classify_error(503).retryable)
        self.assertTrue(yt.classify_error(429).retryable)
        self.assertFalse(yt.classify_error(400).retryable)


class UploadTests(Base):
    def setUp(self):
        super().setUp()
        make_secrets(self.dir)

    def test_upload_is_private_and_uses_job_content(self):
        c = self.client()
        res = yt.upload_private(job(), self.video, self.thumb, client=c)
        self.assertEqual(res["youtube_video_id"], "VID1")
        self.assertEqual(res["privacy"], "private")
        self.assertFalse(res["already_uploaded"])
        init = [x for x in self.t.calls if "uploadType=resumable" in x[1]][0]
        meta = json.loads(init[3])
        self.assertEqual(meta["status"]["privacyStatus"], "private")
        self.assertEqual(meta["snippet"]["title"], "T")
        self.assertEqual(meta["snippet"]["tags"], ["a", "b c"])
        self.assertIn("part=snippet,status", init[1])
        self.assertEqual(self.t.count("POST", "thumbnails/set"), 1)
        self.assertEqual(self.t.count("PUT", "videos?part=status"), 0)

    def test_idempotent_existing_id_makes_no_api_call(self):
        c = self.client()
        res = yt.upload_private(job(vid="OLD"), self.video, self.thumb, client=c)
        self.assertTrue(res["already_uploaded"])
        self.assertEqual(res["youtube_video_id"], "OLD")
        self.assertEqual(self.t.calls, [])

    def test_two_sequential_calls_single_insert_when_job_updated(self):
        c = self.client()
        j = job()
        r1 = yt.upload_private(j, self.video, self.thumb, client=c)
        j2 = job(vid=r1["youtube_video_id"])
        yt.upload_private(j2, self.video, self.thumb, client=c)
        self.assertEqual(self.t.count("POST", "uploadType=resumable"), 1)

    def test_resume_sets_missing_thumbnail_only(self):
        c = self.client()
        yt.upload_private(job(vid="V9", thumb_set=False), self.video, self.thumb, client=c)
        self.assertEqual(self.t.count(None, "uploadType=resumable"), 0)
        self.assertEqual(self.t.count("POST", "thumbnails/set"), 1)

    def test_concurrent_same_key_rejected(self):
        c = self.client()
        with yt._inflight_lock:
            yt._inflight.add("busy")
        try:
            with self.assertRaises(yt.YouTubeError) as cm:
                yt.upload_private(job("busy"), self.video, self.thumb, client=c)
            self.assertEqual(cm.exception.code, "in_progress")
        finally:
            yt._inflight.discard("busy")

    def test_missing_idempotency_key(self):
        with self.assertRaises(yt.YouTubeError):
            yt.upload_private({"content": {}, "upload": {}}, self.video, self.thumb,
                              client=self.client())

    def test_inflight_key_released_after_failure(self):
        c = self.client(lambda *a: resp(400, body={}))
        with self.assertRaises(yt.YouTubeError):
            yt.upload_private(job("kf"), self.video, self.thumb, client=c)
        self.assertNotIn("kf", yt._inflight)

    def test_transient_error_retries_with_bounded_backoff(self):
        n = {"i": 0}

        def handler(method, url, headers, body, t):
            if "uploadType=resumable" in url:
                n["i"] += 1
                if n["i"] <= 2:
                    return resp(503, body={})
            return happy(method, url, headers, body, t)

        c = self.client(handler)
        yt.upload_private(job(), self.video, None, client=c)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_gives_up_after_max_retries_retryable(self):
        c = self.client(lambda *a: resp(503, body={}))
        with self.assertRaises(yt.YouTubeError) as cm:
            yt.upload_private(job(), self.video, None, client=c)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.sleeps), yt.MAX_RETRIES)
        self.assertEqual(self.t.count("POST"), yt.MAX_RETRIES + 1)

    def test_quota_not_retried_inline_but_retryable_flag(self):
        body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
        c = self.client(lambda *a: resp(403, body=body))
        with self.assertRaises(yt.YouTubeError) as cm:
            yt.upload_private(job(), self.video, None, client=c)
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("quota", True))
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.t.calls), 1)

    def test_401_refreshes_once_then_succeeds(self):
        state = {"refreshed": False}

        def handler(method, url, headers, body, t):
            if "oauth2" in url:
                state["refreshed"] = True
                return resp(200, body={"access_token": "FRESH", "expires_in": 3600})
            if "thumbnails/set" in url and headers["Authorization"] != "Bearer FRESH":
                return resp(401, body={})
            return happy(method, url, headers, body, t)

        c = self.client(handler)
        c.set_thumbnail("VID1", self.thumb)
        self.assertTrue(state["refreshed"])

    def test_persistent_401_is_retryable_error(self):
        def handler(method, url, headers, body, t):
            if "oauth2" in url:
                return resp(200, body={"access_token": "F", "expires_in": 3600})
            return resp(401, body={})

        c = self.client(handler)
        with self.assertRaises(yt.YouTubeError) as cm:
            c.set_thumbnail("V", self.thumb)
        self.assertEqual(cm.exception.code, "unauthorized")
        self.assertTrue(cm.exception.retryable)

    def test_network_error_retryable(self):
        def handler(*a):
            raise OSError("down")

        c = self.client(handler)
        with self.assertRaises(yt.YouTubeError) as cm:
            c.set_thumbnail("V", self.thumb)
        self.assertEqual(cm.exception.code, "network")
        self.assertTrue(cm.exception.retryable)

    def test_thumbnail_failure_keeps_video_id_and_calls_on_created(self):
        created = []

        def handler(method, url, headers, body, t):
            if "thumbnails/set" in url:
                return resp(400, body={})
            return happy(method, url, headers, body, t)

        c = self.client(handler)
        with self.assertRaises(yt.YouTubeError) as cm:
            yt.upload_private(job(), self.video, self.thumb, client=c,
                              on_created=created.append)
        self.assertEqual(cm.exception.video_id, "VID1")
        self.assertEqual(created, ["VID1"])

    def test_oversize_thumbnail_rejected_without_call(self):
        big = os.path.join(self._td.name, "big.jpg")
        with open(big, "wb") as fh:
            fh.write(b"0" * (yt.MAX_THUMB_BYTES + 1))
        c = self.client()
        with self.assertRaises(yt.YouTubeError):
            c.set_thumbnail("V", big)
        self.assertEqual(self.t.calls, [])

    def test_chunked_upload_resumes_after_failure(self):
        sent = []
        state = {"fail": True}

        def handler(method, url, headers, body, t):
            if "uploadType=resumable" in url:
                return resp(200, {"Location": "https://upload.example/s1"})
            if "upload.example" in url:
                rng = headers["Content-Range"]
                if rng.startswith("bytes */"):
                    return resp(308, {"Range": "bytes=0-399"})
                sent.append(rng)
                if state["fail"] and rng.startswith("bytes 400-"):
                    state["fail"] = False
                    return resp(503, body={})
                end = int(rng.split("-")[1].split("/")[0])
                if end + 1 >= 1000:
                    return resp(200, body={"id": "VIDC"})
                return resp(308, {"Range": "bytes=0-%d" % end})
            return resp(404)

        c = self.client(handler)
        with mock.patch.object(yt, "CHUNK_SIZE", 400):
            vid = c.insert_private(self.video, {"title": "t"})
        self.assertEqual(vid, "VIDC")
        self.assertEqual(sent, ["bytes 0-399/1000", "bytes 400-799/1000",
                                "bytes 400-799/1000", "bytes 800-999/1000"])

    def test_session_expired_is_retryable(self):
        def handler(method, url, headers, body, t):
            if "uploadType=resumable" in url:
                return resp(200, {"Location": "https://upload.example/s1"})
            return resp(404, body={})

        c = self.client(handler)
        with self.assertRaises(yt.YouTubeError) as cm:
            c.insert_private(self.video, {"title": "t"})
        self.assertEqual(cm.exception.code, "session_expired")
        self.assertTrue(cm.exception.retryable)

    def test_no_secret_or_token_in_error_text(self):
        c = self.client(lambda *a: resp(500, body={"error": {"message": "x"}}))
        with self.assertRaises(yt.YouTubeError) as cm:
            c.set_thumbnail("V", self.thumb)
        text = str(cm.exception)
        for secret in ("csec", "RT", "AT"):
            self.assertNotIn(secret, text.replace("YouTube", ""))


class PublicTests(Base):
    def setUp(self):
        super().setUp()
        make_secrets(self.dir)

    def test_set_public_merges_status(self):
        c = self.client()
        res = yt.set_public("VID1", client=c)
        self.assertEqual(res["privacy"], "public")
        put = [x for x in self.t.calls if x[0] == "PUT"][0]
        payload = json.loads(put[3])
        self.assertEqual(payload["status"]["privacyStatus"], "public")
        self.assertIn("selfDeclaredMadeForKids", payload["status"])

    def test_upload_never_makes_public(self):
        c = self.client()
        yt.upload_private(job(), self.video, self.thumb, client=c)
        for method, url, _, body in self.t.calls:
            if body and b"privacyStatus" in body and method in ("POST", "PUT"):
                self.assertNotIn(b"public", body)
        self.assertEqual(self.t.count("PUT", "videos?part=status"), 0)

    def test_set_public_requires_id(self):
        with self.assertRaises(yt.YouTubeError):
            yt.set_public("", client=self.client())

    def test_set_public_unknown_video(self):
        c = self.client(lambda *a: resp(200, body={"items": []}))
        with self.assertRaises(yt.YouTubeError):
            c.set_public("nope")


if __name__ == "__main__":
    unittest.main()
