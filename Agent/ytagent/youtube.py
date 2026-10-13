"""YouTube Data API v3 (stdlib only): OAuth2 loopback flow, resumable private
upload, thumbnails.set, and the single go-public call.

Public API
----------
status(secrets_dir=None) -> {"configured": bool, "authorized": bool, "reason": str}
    Honest integration state; never exposes secrets.
configure(secrets_dir) -> None            set default secrets directory.
authorize(secrets_dir=None, open_browser=webbrowser.open, timeout=300,
          transport=None) -> dict          run the OAuth2 loopback flow, saves token.json.
upload_private(job, video_path, thumb_path, client=None, on_created=None) -> dict
    Idempotent by job["upload"]["idempotency_key"]; always privacyStatus=private.
    Returns {"youtube_video_id","privacy","uploaded_at","already_uploaded","thumbnail_set"}.
    The caller persists the result into job["upload"]. `on_created(video_id)` is
    invoked right after videos.insert so the id can be saved before the thumbnail step.
set_public(video_id, client=None) -> dict  ONLY to be called from go-public.
YouTubeClient(secrets_dir, transport=None, sleep=time.sleep, clock=time.time)
    .insert_private(...), .set_thumbnail(...), .set_public(...)
YouTubeError(message, retryable, code, status, video_id)
    code in: not_configured | not_authorized | reauth_required | unauthorized |
             quota | transient | network | forbidden | invalid | session_expired |
             in_progress | thumbnail
Transport: callable(method, url, headers, body) -> (status, headers, body_bytes);
    inject a fake in tests. Secrets/tokens are never logged or put in error text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets as _secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
)
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
CHUNK_SIZE = 8 * 1024 * 1024  # multiple of 256 KiB
MAX_RETRIES = 4
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
MAX_THUMB_BYTES = 2 * 1024 * 1024
DEFAULT_CATEGORY = "22"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                 "userRateLimitExceeded"}
STUDIO_HINT = ("Bạn vẫn có thể tải video lên thủ công qua YouTube Studio "
               "(studio.youtube.com) bằng tiêu đề/mô tả/thẻ đã chuẩn bị.")

_default_secrets_dir = os.path.join(os.getcwd(), "secrets")
_inflight = set()
_inflight_lock = threading.Lock()


class YouTubeError(Exception):
    def __init__(self, message, retryable=False, code="invalid", status=None,
                 video_id=None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code
        self.status = status
        self.video_id = video_id


def configure(secrets_dir):
    global _default_secrets_dir
    _default_secrets_dir = str(secrets_dir)


def _dir(secrets_dir):
    return str(secrets_dir) if secrets_dir else _default_secrets_dir


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- transport
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(method, url, headers, body):
    """Default transport; returns (status, lowercase-headers, bytes)."""
    if not url.lower().startswith("https://"):
        raise YouTubeError("Chỉ cho phép HTTPS.", False, "invalid")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        data = exc.read() if exc.fp else b""
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, data


# ------------------------------------------------------------ secrets files
def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_client_config(secrets_dir=None):
    """Return the 'installed'/'web' section of client_secret.json or raise."""
    path = os.path.join(_dir(secrets_dir), CLIENT_SECRET_FILE)
    if not os.path.isfile(path):
        raise YouTubeError(
            "Chưa cấu hình YouTube: thiếu secrets/client_secret.json (tải từ Google "
            "Cloud Console, loại OAuth 'Desktop app'). " + STUDIO_HINT,
            False, "not_configured")
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        raise YouTubeError("client_secret.json không đọc được hoặc sai định dạng. "
                           + STUDIO_HINT, False, "not_configured")
    section = data.get("installed") or data.get("web") or {}
    if not section.get("client_id") or not section.get("client_secret"):
        raise YouTubeError("client_secret.json thiếu client_id/client_secret. "
                           + STUDIO_HINT, False, "not_configured")
    return section


def load_token(secrets_dir=None):
    path = os.path.join(_dir(secrets_dir), TOKEN_FILE)
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_token(token, secrets_dir=None):
    """Atomic write with owner-only permissions (best effort on Windows)."""
    directory = _dir(secrets_dir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, TOKEN_FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(token, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def status(secrets_dir=None):
    """Truthful integration status for /api/integrations (no secrets)."""
    try:
        load_client_config(secrets_dir)
    except YouTubeError as exc:
        return {"configured": False, "authorized": False, "reason": exc.message}
    token = load_token(secrets_dir)
    if not token or not token.get("refresh_token"):
        return {"configured": True, "authorized": False,
                "reason": "Đã có client_secret.json nhưng chưa cấp quyền: hãy chạy "
                          "bước 'Kết nối YouTube' để đăng nhập. " + STUDIO_HINT}
    return {"configured": True, "authorized": True, "reason": "Sẵn sàng."}


# -------------------------------------------------------------------- OAuth
def _form(params):
    return urllib.parse.urlencode(params).encode("utf-8")


def _pkce():
    verifier = _secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(client_id, redirect_uri, state, challenge):
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": " ".join(SCOPES), "state": state,
        "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256"})


def _token_request(params, client_cfg, transport, clock):
    params = dict(params, client_id=client_cfg["client_id"],
                  client_secret=client_cfg["client_secret"])
    try:
        status_code, _, body = transport(
            "POST", client_cfg.get("token_uri") or TOKEN_URI,
            {"Content-Type": "application/x-www-form-urlencoded"}, _form(params))
    except (OSError, urllib.error.URLError):
        raise YouTubeError("Không kết nối được Google để lấy token.", True, "network")
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    if status_code != 200 or "access_token" not in data:
        err = data.get("error") if isinstance(data, dict) else None
        if err == "invalid_grant":
            raise YouTubeError("Quyền truy cập YouTube đã hết hạn/bị thu hồi: hãy kết "
                               "nối lại.", False, "reauth_required", status_code)
        retry = status_code in (429, 500, 502, 503, 504)
        raise YouTubeError("Google từ chối yêu cầu token (%s)." % (err or status_code),
                           retry, "transient" if retry else "not_authorized", status_code)
    return {"access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "token_type": data.get("token_type", "Bearer"),
            "scope": data.get("scope", " ".join(SCOPES)),
            "expires_at": clock() + int(data.get("expires_in", 3600))}


def exchange_code(code, redirect_uri, verifier, secrets_dir=None, transport=None,
                  clock=time.time):
    cfg = load_client_config(secrets_dir)
    token = _token_request({"grant_type": "authorization_code", "code": code,
                            "redirect_uri": redirect_uri, "code_verifier": verifier},
                           cfg, transport or urllib_transport, clock)
    if not token.get("refresh_token"):
        raise YouTubeError("Google không trả refresh_token; hãy thử kết nối lại.",
                           False, "not_authorized")
    save_token(token, secrets_dir)
    return token


def authorize(secrets_dir=None, open_browser=webbrowser.open, timeout=300,
              transport=None, clock=time.time):
    """Installed-app loopback flow (127.0.0.1, random port, PKCE + state)."""
    cfg = load_client_config(secrets_dir)
    verifier, challenge = _pkce()
    state = _secrets.token_urlsafe(24)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state:
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
                text = "Đã kết nối YouTube. Bạn có thể đóng tab này."
            else:
                text = "Yêu cầu không hợp lệ."
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # never log URLs (contain the code)
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect_uri = "http://127.0.0.1:%d/" % server.server_address[1]
    try:
        open_browser(build_auth_url(cfg["client_id"], redirect_uri, state, challenge))
        deadline = time.time() + timeout
        while "code" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result.get("code"):
        raise YouTubeError("Chưa nhận được xác nhận đăng nhập từ Google.", True,
                           "not_authorized")
    return exchange_code(result["code"], redirect_uri, verifier, secrets_dir,
                         transport, clock)


# ------------------------------------------------------------------- errors
def _google_reason(body):
    try:
        data = json.loads(body.decode("utf-8"))
        err = data.get("error", {})
        reasons = err.get("errors") or [{}]
        return reasons[0].get("reason", ""), err.get("message", "")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return "", ""


def classify_error(status_code, body=b""):
    """Map an HTTP failure to a YouTubeError (retryable flag + Vietnamese text)."""
    reason, msg = _google_reason(body)
    if status_code == 401:
        return YouTubeError("YouTube từ chối token (401); cần làm mới/kết nối lại.",
                            True, "unauthorized", 401)
    if status_code == 403 and reason in QUOTA_REASONS:
        return YouTubeError("Đã hết hạn mức (quota) YouTube API; thử lại sau.",
                            True, "quota", 403)
    if status_code == 403:
        return YouTubeError("YouTube từ chối quyền thực hiện (%s)." % (reason or 403),
                            False, "forbidden", 403)
    if status_code in (429, 500, 502, 503, 504):
        return YouTubeError("YouTube tạm thời lỗi (%d); sẽ thử lại." % status_code,
                            True, "transient", status_code)
    return YouTubeError("YouTube báo lỗi %d: %s" % (status_code, reason or msg[:120]),
                        False, "invalid", status_code)


# ------------------------------------------------------------------- client
class YouTubeClient:
    def __init__(self, secrets_dir=None, transport=None, sleep=time.sleep,
                 clock=time.time):
        self.secrets_dir = _dir(secrets_dir)
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()

    # -- auth
    def _access_token(self, force=False):
        with self._lock:
            token = load_token(self.secrets_dir)
            if not token or not token.get("refresh_token"):
                raise YouTubeError("Chưa kết nối YouTube (thiếu token). "
                                   + STUDIO_HINT, False, "not_authorized")
            if not force and token.get("expires_at", 0) - 60 > self.clock():
                return token["access_token"]
            cfg = load_client_config(self.secrets_dir)
            fresh = _token_request({"grant_type": "refresh_token",
                                    "refresh_token": token["refresh_token"]},
                                   cfg, self.transport, self.clock)
            fresh["refresh_token"] = fresh.get("refresh_token") or token["refresh_token"]
            save_token(fresh, self.secrets_dir)
            return fresh["access_token"]

    def _backoff(self, attempt):
        self.sleep(min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt)))

    # -- HTTP with retry
    def _request(self, method, url, headers=None, body=None, accept=(200, 201),
                 retries=MAX_RETRIES):
        refreshed = False
        attempt = 0
        while True:
            hdrs = dict(headers or {})
            hdrs["Authorization"] = "Bearer " + self._access_token()
            try:
                code, rh, rb = self.transport(method, url, hdrs, body)
                err = None if code in accept else classify_error(code, rb)
            except (OSError, urllib.error.URLError):
                code, rh, rb = None, {}, b""
                err = YouTubeError("Mất kết nối mạng tới YouTube.", True, "network")
            if err is None:
                return code, rh, rb
            if err.code == "unauthorized" and not refreshed:
                refreshed = True
                self._access_token(force=True)
                continue
            if err.retryable and err.code in ("transient", "network") \
                    and attempt < retries:
                self._backoff(attempt)
                attempt += 1
                continue
            raise err

    @staticmethod
    def _json(body):
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise YouTubeError("Phản hồi YouTube không hợp lệ.", True, "transient")

    # -- operations
    def insert_private(self, video_path, snippet, made_for_kids=False,
                       on_created=None):
        """videos.insert via resumable upload; privacyStatus is hard-coded private."""
        size = os.path.getsize(video_path)
        if size <= 0:
            raise YouTubeError("File video rỗng.", False, "invalid")
        ctype = mimetypes.guess_type(video_path)[0] or "application/octet-stream"
        meta = {"snippet": snippet,
                "status": {"privacyStatus": "private",
                           "selfDeclaredMadeForKids": bool(made_for_kids)}}
        _, rh, _ = self._request(
            "POST", UPLOAD_API + "/videos?uploadType=resumable&part=snippet,status",
            {"Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": ctype},
            json.dumps(meta).encode("utf-8"))
        session = rh.get("location")
        if not session:
            raise YouTubeError("YouTube không trả địa chỉ upload.", True, "transient")
        data = self._json(self._send_file(session, video_path, size, ctype))
        video_id = data.get("id")
        if not video_id:
            raise YouTubeError("YouTube không trả video id.", True, "transient")
        if on_created:
            on_created(video_id)
        return video_id

    def _send_file(self, session, path, size, ctype):
        offset = 0
        attempt = 0
        with open(path, "rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK_SIZE)
                end = offset + len(chunk) - 1
                headers = {"Content-Type": ctype, "Content-Length": str(len(chunk)),
                           "Content-Range": "bytes %d-%d/%d" % (offset, end, size)}
                try:
                    code, rh, rb = self._request("PUT", session, headers, chunk,
                                                 accept=(200, 201, 308), retries=0)
                except YouTubeError as exc:
                    if exc.code == "invalid" and exc.status in (404, 410):
                        raise YouTubeError("Phiên upload hết hạn; cần upload lại.",
                                           True, "session_expired", exc.status)
                    if not (exc.retryable and exc.code in ("transient", "network")) \
                            or attempt >= MAX_RETRIES:
                        raise
                    self._backoff(attempt)
                    attempt += 1
                    offset, done = self._query_offset(session, size, offset)
                    if done is not None:
                        return done
                    continue
                attempt = 0
                if code in (200, 201):
                    return rb
                offset = self._parse_range(rh, offset)

    def _query_offset(self, session, size, fallback):
        try:
            code, rh, rb = self._request(
                "PUT", session, {"Content-Range": "bytes */%d" % size,
                                 "Content-Length": "0"},
                b"", accept=(200, 201, 308), retries=0)
        except YouTubeError:
            return fallback, None
        if code in (200, 201):
            return size, rb
        return self._parse_range(rh, fallback), None

    @staticmethod
    def _parse_range(headers, default):
        rng = headers.get("range", "")
        if rng.startswith("bytes=0-"):
            try:
                return int(rng.rsplit("-", 1)[1]) + 1
            except ValueError:
                return default
        return 0 if "range" not in headers else default

    def set_thumbnail(self, video_id, thumb_path):
        size = os.path.getsize(thumb_path)
        if size > MAX_THUMB_BYTES:
            raise YouTubeError("Thumbnail vượt 2MB, YouTube sẽ từ chối.", False,
                               "thumbnail", video_id=video_id)
        ctype = mimetypes.guess_type(thumb_path)[0] or "image/jpeg"
        with open(thumb_path, "rb") as fh:
            data = fh.read()
        url = UPLOAD_API + "/thumbnails/set?uploadType=media&videoId=" \
            + urllib.parse.quote(video_id, safe="")
        self._request("POST", url, {"Content-Type": ctype,
                                    "Content-Length": str(len(data))}, data)

    def set_public(self, video_id):
        """videos.update -> public. Merges the current status to keep other flags."""
        q = urllib.parse.quote(video_id, safe="")
        _, _, body = self._request("GET", "%s/videos?part=status&id=%s" % (API, q))
        items = self._json(body).get("items") or []
        if not items:
            raise YouTubeError("Không tìm thấy video trên YouTube.", False, "invalid",
                               404, video_id)
        st = dict(items[0].get("status") or {})
        st["privacyStatus"] = "public"
        st.pop("publishAt", None)
        payload = {"id": video_id, "status": st}
        _, _, rb = self._request(
            "PUT", API + "/videos?part=status",
            {"Content-Type": "application/json; charset=UTF-8"},
            json.dumps(payload).encode("utf-8"))
        return self._json(rb)


# ------------------------------------------------------- job level helpers
def _snippet_from_job(job):
    content = job.get("content") or {}
    return {"title": content.get("title", ""),
            "description": content.get("description", ""),
            "tags": list(content.get("tags") or []),
            "categoryId": str(content.get("category_id") or DEFAULT_CATEGORY)}


def upload_private(job, video_path, thumb_path, client=None, on_created=None):
    """Idempotent private upload. See module docstring."""
    upload = job.get("upload") or {}
    key = upload.get("idempotency_key")
    if not key:
        raise YouTubeError("Job thiếu idempotency_key.", False, "invalid")
    existing = upload.get("youtube_video_id")
    client = client or YouTubeClient()
    if existing:
        thumb_done = upload.get("thumbnail_set", True)
        if not thumb_done and thumb_path:
            client.set_thumbnail(existing, thumb_path)
        return {"youtube_video_id": existing, "privacy": upload.get("privacy") or "private",
                "uploaded_at": upload.get("uploaded_at") or _now_iso(),
                "already_uploaded": True, "thumbnail_set": True}
    with _inflight_lock:
        if key in _inflight:
            raise YouTubeError("Đang upload job này; vui lòng chờ.", True, "in_progress")
        _inflight.add(key)
    try:
        content = job.get("content") or {}
        video_id = client.insert_private(
            video_path, _snippet_from_job(job),
            made_for_kids=bool(content.get("made_for_kids", False)),
            on_created=on_created)
        try:
            if thumb_path:
                client.set_thumbnail(video_id, thumb_path)
        except YouTubeError as exc:
            exc.video_id = video_id  # video exists (private); caller keeps the id
            raise
        return {"youtube_video_id": video_id, "privacy": "private",
                "uploaded_at": _now_iso(), "already_uploaded": False,
                "thumbnail_set": bool(thumb_path)}
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def set_public(video_id, client=None):
    """Make a video public. Must only be called from the go-public handler."""
    if not video_id or not isinstance(video_id, str):
        raise YouTubeError("Thiếu video id.", False, "invalid")
    client = client or YouTubeClient()
    client.set_public(video_id)
    return {"youtube_video_id": video_id, "privacy": "public",
            "confirmed_at": _now_iso()}
