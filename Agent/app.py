from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
CONFIG_PATH = ROOT / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
OUTBOX = (ROOT / CONFIG.get("outbox", "outbox")).resolve()
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PLATFORMS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "facebook": "Facebook Reels",
}
MAX_VIDEO_MB = 4096
INDEX_LOCK = threading.Lock()
CACHE = {"at": 0, "videos": []}

STATIC = ROOT / "static"
MAX_BODY = 256 * 1024
LOCAL_IPS = ("127.0.0.1", "::1")
MIME = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
API = None


def ollama_info() -> dict:
    base = CONFIG.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            models = json.loads(r.read().decode()).get("models", [])
        return {"available": bool(models), "model": CONFIG.get("ollama_model") or (models[0].get("name") if models else "")}
    except Exception:
        return {"available": False, "model": ""}


def get_api():
    global API
    if API is None:
        from ytagent import routes, youtube
        youtube.configure(str(ROOT / "secrets"))
        API = routes.Api(OUTBOX, load_roots, find_video, ollama_status=ollama_info)
    return API


def load_roots() -> list[Path]:
    roots = []
    for value in CONFIG.get("video_roots", []):
        path = (ROOT / value).resolve()
        if path.exists() and path.is_dir():
            roots.append(path)
    return roots


def make_id(path: Path) -> str:
    import hashlib
    return hashlib.sha1(str(path.resolve()).lower().encode("utf-8")).hexdigest()[:16]


def scan_videos(force=False) -> list[dict]:
    with INDEX_LOCK:
        if not force and time.time() - CACHE["at"] < 45:
            return CACHE["videos"]
        items = []
        for root in load_roots():
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                    continue
                try:
                    st = path.stat()
                    if st.st_size > MAX_VIDEO_MB * 1024 * 1024:
                        warning = f"File over {MAX_VIDEO_MB} MB"
                    else:
                        warning = None
                    info = {"id": make_id(path), "name": path.stem, "file": path.name,
                            "folder": str(path.parent.relative_to(WORKSPACE)), "ext": path.suffix.lower(),
                            "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "warning": warning, "path": str(path)}
                    if cv2 is not None:
                        cap = cv2.VideoCapture(str(path))
                        if cap.isOpened():
                            info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                            info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                            info["duration"] = round(cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(cap.get(cv2.CAP_PROP_FPS), 0.01), 2)
                        cap.release()
                    items.append(info)
                except OSError:
                    continue
        items.sort(key=lambda v: (v["modified"], v["name"]), reverse=True)
        CACHE.update(at=time.time(), videos=items)
        return items


def find_video(video_id: str) -> dict | None:
    for v in scan_videos():
        if v["id"] == video_id:
            return v
    return None


def sample_frames(video: dict) -> list[tuple[float, "Image.Image"]]:
    if cv2 is None or Image is None:
        raise RuntimeError("Cần cài opencv-python và Pillow để trích frame.")
    cap = cv2.VideoCapture(video["path"])
    if not cap.isOpened():
        raise RuntimeError("Không đọc được video. Hãy thử file MP4/H.264.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = frame_count / fps if frame_count else 0
    points = [duration * f for f in (0.18, 0.38, 0.58, 0.78)] if duration else [0, 1, 2, 3]
    frames = []
    for sec in points:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, sec) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame)
        frames.append((round(sec, 2), image))
    cap.release()
    return frames


def save_frame_assets(video: dict, job_dir: Path) -> list[dict]:
    frames = sample_frames(video)
    assets = []
    for i, (sec, image) in enumerate(frames, 1):
        raw = job_dir / f"frame-{i:02}.jpg"
        image.save(raw, quality=92, optimize=True)
        for name, size in (("youtube-landscape", (1280, 720)), ("vertical-cover", (1080, 1920)), ("square-preview", (1080, 1080))):
            canvas = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.44))
            canvas.save(job_dir / f"{name}-{i:02}.jpg", quality=91, optimize=True)
        assets.append({"time_seconds": sec, "file": raw.name, "variants": [f"youtube-landscape-{i:02}.jpg", f"vertical-cover-{i:02}.jpg", f"square-preview-{i:02}.jpg"]})
    return assets


def ollama_generate(prompt: str) -> tuple[str | None, str]:
    base = CONFIG.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    model = (os.getenv("AGENT_OLLAMA_MODEL") or CONFIG.get("ollama_model") or "").strip()
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=2) as resp:
            models = json.loads(resp.read().decode("utf-8")).get("models", [])
        if not model and models:
            model = models[0].get("name", "")
        if not model:
            return None, "Ollama chạy nhưng chưa có model văn bản."
        body = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.4}}).encode()
        req = urllib.request.Request(base + "/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=150) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("response", "").strip(), f"Ollama local ({model})"
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"Ollama chưa sẵn sàng ({type(exc).__name__})."


def platform_drafts(video: dict, selected: list[str], context: str, fmt: str, schedule: str, generated: dict | None):
    name = video["name"]
    seed = generated or {}
    short_context = context.strip() or f"Chủ đề video: {name}. Kể chuyện chân thực, cảm xúc, không thêm sự kiện ngoài video."
    compact = re.sub(r"[^\wÀ-ỹ]+", "", name, flags=re.UNICODE)[:28]
    topic_tags = {
        "youtube": [f"#{compact}" if compact else "#PhimNgan", "#LuaChon", "#Shorts"],
        "tiktok": [f"#{compact}" if compact else "#KeChuyen", "#ChuyenDoi", "#TamSu"],
        "instagram": [f"#{compact}" if compact else "#PhimNgan", "#PhimNgan", "#SongCham", "#ChuyenDoi"],
        "facebook": [f"#{compact}" if compact else "#PhimNgan", "#ChuyenDoi", "#KeChuyen"],
    }
    fallback_copy = {
        "youtube": f"{short_context}\n\nMột câu chuyện ngắn để cùng suy ngẫm về lựa chọn của mình.",
        "tiktok": f"Có những ngã rẽ khiến mình phải dừng lại và nghĩ.\n\n{short_context}",
        "instagram": f"{short_context}\n\nĐôi khi, nhìn lại một lựa chọn cũng là cách hiểu mình hơn.\n\nBạn đang đứng trước ngã rẽ nào?",
        "facebook": f"{short_context}\n\nBạn nghĩ điều gì giúp mình biết nên chọn hướng đi nào?",
    }
    drafts = []
    for platform in selected:
        title = seed.get(platform, {}).get("title") or name
        caption = seed.get(platform, {}).get("caption") or fallback_copy[platform]
        hashtags = seed.get(platform, {}).get("hashtags") or topic_tags[platform]
        if not isinstance(hashtags, list):
            hashtags = re.findall(r"#[\wÀ-ỹ]+", str(hashtags))
        rules = {
            "youtube": "Viết tiêu đề dễ tìm nhưng tự nhiên, mô tả có ngữ cảnh và CTA nhẹ; tag không phải hashtag. Không tự thêm điểm VidIQ/volume.",
            "tiktok": "Caption ngắn, hook ở câu đầu, giọng tự nhiên, tối đa 4 hashtag phù hợp; không nhồi #fyp.",
            "instagram": "Caption có ngắt dòng, cảm xúc/ý nghĩa, CTA lưu hoặc chia sẻ nhẹ; 3-5 hashtag niche.",
            "facebook": "Caption mở đầu rõ câu chuyện, có ngữ cảnh để xem không bật tiếng; 2-4 hashtag liên quan, CTA trò chuyện.",
        }[platform]
        youtube_tags = []
        drafts.append({"platform": platform, "platform_name": PLATFORMS[platform], "status": "Chờ duyệt · chưa upload",
                       "title": title[:100], "caption": caption, "hashtags": hashtags[:8], "keywords": [],
                       "tags": youtube_tags, "tag_note": "Bắt buộc: tra cứu VidIQ cho đúng video, dán bộ tags vào đây rồi mới lưu gói duyệt.",
                       "format": fmt, "scheduled_time": schedule or None,
                       "thumbnail_note": "Chọn một ảnh bìa từ frame/crop phía dưới; AI image-to-image chưa cấu hình." if platform != "tiktok" else "Chọn mốc frame làm cover TikTok; ảnh bìa tùy chỉnh chưa được tạo.",
                       "vidiq_note": "Cần kiểm tra trực tiếp trong vidIQ; chưa có số liệu xác thực cho bản nháp này." if platform == "youtube" else "Không dùng điểm VidIQ cho nền tảng này.",
                       "policy_note": "Cần người dùng xác nhận quyền nhạc/hình ảnh, nội dung tài trợ/AI và quy định cộng đồng. Bộ lọc tự động chưa xác nhận tuân thủ.",
                       "copy_rule": rules})
    return drafts


def check_video(video: dict, job_dir: Path) -> list[str]:
    checks = []
    if Path(video["path"]).exists() and video["size"] > 100_000:
        checks.append("file")
    sample = job_dir / "frame-01.jpg"
    if sample.exists():
        checks.append("metadata")
    # These are never auto-cleared; the uploader must certify them.
    return checks


class Handler(BaseHTTPRequestHandler):
    server_version = "YoutubeAgentPilot/0.1"

    def log_message(self, fmt, *args):
        print("[agent] " + fmt % args)

    def _json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, raw: bytes, ctype: str, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: Path):
        """Phục vụ file trong OUTBOX; từ chối secrets/token/client_secret."""
        from ytagent import paths
        try:
            rel = path.resolve().relative_to(OUTBOX.resolve()).as_posix()
        except ValueError:
            return self._json({"error": "Không phục vụ file ngoài outbox."}, 403)
        if paths.is_secret_path(rel):
            return self._json({"error": "File này không được phép truy cập."}, 403)
        if not path.is_file():
            return self._json({"error": "Không tìm thấy file."}, 404)
        typ = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def _static(self, rel: str):
        from ytagent import paths
        try:
            path = paths.safe_join(STATIC, rel)
        except paths.PathError:
            return self._json({"error": "Đường dẫn không hợp lệ."}, 400)
        if not path.is_file():
            return self._json({"error": "Không tìm thấy file."}, 404)
        ctype = MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        extra = {"Content-Security-Policy": CSP} if path.suffix.lower() == ".html" else None
        self._send_bytes(path.read_bytes(), ctype, extra)

    def _api(self, method: str, parsed):
        """Điều phối tới ytagent.routes; POST/PUT chỉ từ máy local, body <= 256KB."""
        body = None
        if method != "GET":
            if self.client_address[0] not in LOCAL_IPS:
                return self._json({"error": "Chỉ cho phép thao tác từ máy local.", "code": "forbidden"}, 403)
            host, origin = self.headers.get("Host", ""), self.headers.get("Origin")
            if origin and urlparse(origin).netloc != host:
                return self._json({"error": "Nguồn yêu cầu (Origin) không hợp lệ.", "code": "forbidden"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                return self._json({"error": "Content-Length không hợp lệ.", "code": "invalid"}, 400)
            if length > MAX_BODY:
                return self._json({"error": "Request quá lớn (tối đa 256KB).", "code": "invalid"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json({"error": "JSON không hợp lệ.", "code": "invalid"}, 400)
        status, payload = get_api().handle(method, parsed.path, parse_qs(parsed.query), body)
        self._json(payload, status)

    def do_PUT(self):
        parsed = urlparse(self.path)
        if get_api().owns(parsed.path):
            return self._api("PUT", parsed)
        self._json({"error": "Route không tồn tại."}, 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._static("index.html")
        if parsed.path.startswith("/static/"):
            return self._static(unquote(parsed.path[len("/static/"):]))
        if get_api().owns(parsed.path):
            return self._api("GET", parsed)
        if parsed.path == "/api/videos":
            videos = scan_videos(force=True)
            safe = [{k: v for k, v in x.items() if k != "path"} for x in videos]
            return self._json({"videos": safe, "roots": [str(p.relative_to(WORKSPACE)) for p in load_roots()]})
        if parsed.path == "/api/health":
            base = CONFIG.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
            models = []
            try:
                with urllib.request.urlopen(base + "/api/tags", timeout=2) as r:
                    models = json.loads(r.read().decode()).get("models", [])
            except Exception:
                pass
            comfy = CONFIG.get("comfyui_url", "").rstrip("/")
            comfy_ok = False
            if comfy:
                try:
                    with urllib.request.urlopen(comfy + "/system_stats", timeout=2):
                        comfy_ok = True
                except Exception:
                    pass
            return self._json({"ollama": bool(models), "model": (os.getenv("AGENT_OLLAMA_MODEL") or CONFIG.get("ollama_model") or (models[0].get("name") if models else "")), "comfyui": comfy_ok,
                               "note": "GPU RTX 3080 Ti thấy được; Python hiện là torch CPU-only. Tạo ảnh AI local cần cài workflow/model tương thích."})
        if parsed.path == "/api/frames":
            query = parse_qs(parsed.query)
            video = find_video(query.get("id", [""])[0])
            if not video:
                return self._json({"error": "Video không còn trong danh sách."}, 404)
            try:
                frames = sample_frames(video)
                # Preview source frame in-memory; no copying or modifying source video.
                jobdir = OUTBOX / ".preview"
                jobdir.mkdir(parents=True, exist_ok=True)
                items = []
                for i, (sec, img) in enumerate(frames, 1):
                    target = jobdir / f"{video['id']}-{i:02}.jpg"
                    img.thumbnail((480, 480))
                    img.save(target, quality=84)
                    items.append({"time": f"{sec:.1f}s", "url": f"/api/file?path={quote(str(target.relative_to(OUTBOX)))}"})
                return self._json({"frames": items})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if parsed.path == "/api/file":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            from ytagent import paths
            try:
                return self._file(paths.safe_join(OUTBOX, rel))
            except paths.PathError:
                return self._json({"error": "Đường dẫn không hợp lệ."}, 400)
        match = re.fullmatch(r"/api/job/([0-9a-f-]+)/json", parsed.path)
        if match:
            return self._file(OUTBOX / match.group(1) / "review.json")
        self._json({"error": "Route không tồn tại."}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if get_api().owns(route):
            return self._api("POST", parsed)
        if route.startswith("/api/job/") and route.endswith("/draft"):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "Chỉ cho phép thao tác từ máy local."}, 403)
            match = re.fullmatch(r"/api/job/([0-9a-f-]+)/draft", route)
            if not match:
                return self._json({"error": "Mã gói không hợp lệ."}, 400)
            job_id = match.group(1)
            path = OUTBOX / job_id / "review.json"
            if not path.is_file():
                return self._json({"error": "Không tìm thấy gói duyệt."}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 128_000:
                    return self._json({"error": "Request quá lớn."}, 413)
                data = json.loads(self.rfile.read(length) or b"{}")
                job = json.loads(path.read_text(encoding="utf-8"))
                edits = {x.get("platform"): x for x in data.get("platforms", []) if isinstance(x, dict)}
                for platform in job.get("platforms", []):
                    edit = edits.get(platform.get("platform"))
                    if not edit:
                        continue
                    platform["title"] = str(edit.get("title", platform["title"]))[:100]
                    platform["caption"] = str(edit.get("caption", platform["caption"]))[:10_000]
                    raw_tags = edit.get("hashtags", [])
                    if isinstance(raw_tags, str):
                        raw_tags = raw_tags.split()
                    if platform.get("platform") == "youtube" and not raw_tags:
                        return self._json({"error": "Bắt buộc tra cứu VidIQ và nhập tags YouTube cho video này trước khi lưu."}, 400)
                    platform["hashtags"] = [str(x)[:100] for x in raw_tags[:12]]
                    if platform.get("platform") == "youtube":
                        platform["tags"] = [str(x).strip()[:100] for x in raw_tags[:30] if str(x).strip()]
                job["edited_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                job["status"] = "review_only_not_uploaded"
                path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                job["folder_url"] = f"/api/file?path={quote(job_id + '/review.json')}"
                return self._json(job)
            except Exception as exc:
                return self._json({"error": f"Không lưu được gói duyệt: {exc}"}, 400)
        if route != "/api/prepare":
            return self._json({"error": "Route không tồn tại."}, 404)
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json({"error": "Chỉ cho phép thao tác từ máy local."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 128_000:
                return self._json({"error": "Request quá lớn."}, 413)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "JSON không hợp lệ."}, 400)
        video = find_video(str(data.get("id", "")))
        if not video:
            return self._json({"error": "Video không được tìm thấy trong thư mục nguồn."}, 404)
        selected = [p for p in data.get("platforms", []) if p in PLATFORMS]
        if not selected:
            return self._json({"error": "Chọn ít nhất một nền tảng."}, 400)
        job_id = str(uuid.uuid4())
        job_dir = OUTBOX / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        try:
            assets = save_frame_assets(video, job_dir)
        except Exception as exc:
            return self._json({"error": f"Không trích được frame: {exc}"}, 500)
        selected_context = str(data.get("context", ""))[:4000]
        prompt = ("Trả lời thuần JSON object với khóa platforms, mỗi khóa youtube/tiktok/instagram/facebook có title, caption, hashtags array. "
                  "Viết nội dung phù hợp nền tảng, bằng tiếng Việt, trung thực, không bịa nội dung ngoài tên video/ngữ cảnh. "
                  f"Video: {video['name']}\nNgữ cảnh người dùng: {selected_context}\nNền tảng: {', '.join(selected)}\n"
                  "YouTube title tối đa 100 ký tự; TikTok caption ngắn; Instagram giàu cảm xúc và CTA nhẹ; Facebook dễ đọc, gợi trò chuyện. "
                  "Hashtag liên quan, không spam.")
        output, engine = ollama_generate(prompt)
        generated = None
        if output:
            try:
                block = re.search(r"\{[\s\S]*\}", output)
                generated = json.loads(block.group(0)) if block else None
                generated = (generated or {}).get("platforms", generated)
            except (json.JSONDecodeError, TypeError):
                generated = None
                engine += " · JSON model không đọc được, dùng template"
        drafts = platform_drafts(video, selected, selected_context, str(data.get("format", "short")), str(data.get("schedule", ""))[:100], generated)
        checks = check_video(video, job_dir)
        job = {"id": job_id, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
               "status": "review_only_not_uploaded", "video": {k: v for k, v in video.items() if k != "path"},
               "platforms": drafts, "thumbnail_assets": assets, "checks": checks,
               "policy_review": {"status": "human_review_required", "automatic_clearance": False,
                                 "items": ["Quyền/giấy phép nhạc, hình ảnh, footage", "Nội dung cộng đồng và thông tin nhạy cảm",
                                           "Nội dung tài trợ / branded content", "AI disclosure nếu nội dung bị chỉnh sửa tổng hợp thực tế",
                                           "Đối tượng trẻ em / made for kids", "Không đảm bảo pass kiểm duyệt của nền tảng"]},
               "vidiq": {"status": "not_connected", "note": "Mở VidIQ để xác nhận title score, keywords và thời điểm. Không tạo số liệu giả."},
               "ai_engine": engine,
               "technical": {"aspect_ratio": "9:16" if video.get("height", 0) > video.get("width", 0) else "16:9_or_other",
                             "duration_seconds": video.get("duration"), "size_mb": round(video["size"] / 1024 / 1024, 2),
                             "warnings": [video["warning"]] if video.get("warning") else []}}
        (job_dir / "review.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        (job_dir / "README.txt").write_text("Bản xem trước local; chưa upload/đăng. Kiểm tra caption, thumbnail, quyền nội dung và chính sách từng nền tảng trước khi phát hành.\n", encoding="utf-8")
        job["folder_url"] = f"/api/file?path={quote(job_id + '/review.json')}"
        return self._json(job, 201)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    OUTBOX.mkdir(parents=True, exist_ok=True)
    host = os.getenv("AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("AGENT_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Agent publishing pilot: http://{host}:{port}")
    print(f"Video roots: {[str(p) for p in load_roots()]}")
    print("Local mode: upload YouTube chỉ chạy khi đã cấu hình OAuth trong secrets/ (xem README).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local pilot.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
