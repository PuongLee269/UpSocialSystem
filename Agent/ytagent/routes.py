"""Router API JSON mới (tách khỏi HTTP để dễ test).

API:
    Api(outbox, roots, find_video, yt=youtube, client=None, yt_status=None, ollama_status=None)
        roots() -> list[Path]; find_video(video_id) -> dict|None (có khóa 'path').
        .handle(method, path, query: dict[str,list[str]], body: dict|None) -> (status, payload)
        .owns(path) -> bool   # path có thuộc API này không
Lỗi trả {error, code, reasons}; xem workflow.ApiError.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from urllib.parse import quote, unquote

from . import jobs as jobs_mod
from . import paths, thumbnail
from . import youtube as youtube_mod
from .workflow import ApiError, UploadFlow, sync_status, vidiq_brief

PREFIXES = ("/api/jobs", "/api/vidiq", "/api/integrations", "/api/templates")
VIDIQ_URL = "https://app.vidiq.com/"
AI_FLAGS = ("ai_content", "ai_thumbnail", "ai_text")
CONTACT_N = 12
_JOB = r"/api/jobs/([A-Za-z0-9_-]{8,64})"
_CROP_KEYS = ("x", "y", "w", "h")


def _file_url(job_id: str, rel: str) -> str:
    return "/api/file?path=" + quote(f"jobs/{job_id}/{rel}", safe="/")


def _need_dict(value, label="data") -> dict:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid", f"Trường '{label}' phải là object.")
    return value


def _clean_section(section: str, data: dict) -> dict:
    data = dict(_need_dict(data))
    if section == "frame":
        if data.get("selected_index") is not None and (
                not isinstance(data["selected_index"], int) or isinstance(data["selected_index"], bool)):
            raise ApiError(400, "invalid", "selected_index phải là số nguyên.")
        crop = data.get("crop169")
        if crop is not None:
            crop = _need_dict(crop, "crop169")
            try:
                crop = {k: float(crop[k]) for k in _CROP_KEYS} | ({"pos": float(crop["pos"])} if "pos" in crop else {})
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, "invalid", "crop169 cần x, y, w, h dạng số (tỉ lệ 0–1).") from None
            if not all(0 <= crop[k] <= 1 for k in _CROP_KEYS) or crop["w"] <= 0 or crop["h"] <= 0:
                raise ApiError(400, "invalid", "crop169 phải là tỉ lệ trong khoảng 0–1.")
            data["crop169"] = crop
    elif section == "checklist":
        for key, value in data.items():
            if key != "ai_flags" and not isinstance(value, bool):
                raise ApiError(400, "invalid", f"Checklist '{key}' phải là true/false.")
        flags = data.get("ai_flags", [])
        if not isinstance(flags, list) or any(f not in AI_FLAGS for f in flags):
            raise ApiError(400, "invalid", "ai_flags chỉ gồm: " + ", ".join(AI_FLAGS) + ".")
    elif section == "content":
        if "made_for_kids" in data and not isinstance(data["made_for_kids"], bool):
            raise ApiError(400, "invalid", "made_for_kids phải là true/false.")
        if "tags" in data and not isinstance(data["tags"], list):
            raise ApiError(400, "invalid", "tags phải là danh sách.")
        data.pop("tags_total_chars", None)
    elif section == "vidiq" and "confirmed_by_user" in data and not isinstance(data["confirmed_by_user"], bool):
        raise ApiError(400, "invalid", "confirmed_by_user phải là true/false.")
    return data


class Api:
    def __init__(self, outbox, roots, find_video, yt=youtube_mod, client=None, yt_status=None,
                 ollama_status=None):
        self.outbox = Path(outbox)
        self.store = jobs_mod.JobStore(self.outbox)
        self.roots, self.find_video = roots, find_video
        self.yt, self.client = yt, client
        self.yt_status = yt_status or yt.status
        self.ollama_status = ollama_status or (lambda: {"available": None, "note": "Xem /api/health."})
        self._render_lock = threading.Lock()
        self.flow = UploadFlow(self.store, yt, client, lambda: self.yt_status(), self._video_path, self._approved_thumb)

    # ---------- tiện ích ----------
    def owns(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in PREFIXES)

    def _job_dir(self, job_id: str) -> Path:
        return self.outbox / "jobs" / job_id

    def _video_path(self, job: dict) -> Path:
        rel = (job.get("source") or {}).get("rel_path") or ""
        try:
            path = paths.resolve_in_roots(self.roots(), rel)
        except paths.PathError as exc:
            raise ApiError(404, "not_found", f"Không xác định được video nguồn: {exc}") from None
        if not path.is_file():
            raise ApiError(404, "not_found", "File video nguồn không còn tồn tại.")
        return path

    def _approved_thumb(self, job: dict):
        approved = (job.get("thumbnail") or {}).get("approved_version")
        for item in (job.get("thumbnail") or {}).get("versions", []):
            if item.get("v") == approved:
                return paths.safe_join(self._job_dir(job["id"]), item["file"])
        return None

    def _rel_path(self, video: dict) -> str:
        target = Path(video["path"]).resolve()
        for index, root in enumerate(self.roots()):
            try:
                return f"{index}/{target.relative_to(Path(root).resolve()).as_posix()}"
            except ValueError:
                continue
        return ""

    # ---------- điều phối ----------
    def handle(self, method: str, path: str, query: dict, body):
        try:
            return self._route(method, path, query or {}, body)
        except ApiError as exc:
            return exc.status, exc.body()
        except jobs_mod.NotFoundError as exc:
            return 404, {"error": str(exc), "code": "not_found", "reasons": []}
        except jobs_mod.ConflictError as exc:
            return 409, {"error": str(exc), "code": "conflict", "reasons": []}
        except jobs_mod.InvalidError as exc:
            return 400, {"error": str(exc), "code": "invalid", "reasons": []}
        except thumbnail.ThumbnailError as exc:
            return 400, {"error": str(exc), "code": "invalid", "reasons": []}
        except jobs_mod.JobError as exc:
            return 500, {"error": str(exc), "code": "invalid", "reasons": []}

    def _route(self, method, path, query, body):
        if path == "/api/integrations" and method == "GET":
            return 200, {"youtube": self.yt_status(), "vidiq": {"connected": False, "mode": "manual"},
                         "ollama": self.ollama_status()}
        if path == "/api/vidiq/brief" and method == "GET":
            job = self.store.get((query.get("job") or [""])[0])
            return 200, {"text": vidiq_brief(job), "url": VIDIQ_URL}
        if path == "/api/templates" and method == "GET":
            return 200, {"templates": thumbnail.load_templates(self.outbox / "templates.json")}
        m = re.fullmatch(r"/api/templates/([^/]{1,60})", path)
        if m and method == "PUT":
            data = _need_dict((body or {}).get("data"))
            return 200, {"template": thumbnail.save_template(self.outbox / "templates.json", unquote(m.group(1)), data)}
        if path == "/api/jobs":
            if method == "GET":
                return 200, {"jobs": self.store.list()}
            if method == "POST":
                return 201, self._create_job(body or {})
        m = re.fullmatch(_JOB + r"(?:/(.+))?", path)
        if m:
            return self._job_route(method, m.group(1), m.group(2) or "", body or {})
        raise ApiError(404, "not_found", "Route không tồn tại.")

    def _job_route(self, method, job_id, tail, body):
        if tail == "" and method == "GET":
            return 200, self.store.get(job_id)
        if method == "PUT" and tail in jobs_mod.SECTIONS:
            data = _clean_section(tail, body.get("data"))
            self._check_frame_file(job_id, data)
            job = self.store.update_section(job_id, tail, data, body.get("expected_revision"))
            return 200, sync_status(self.store, job)
        if tail == "contact-sheet" and method == "GET":
            return 200, {"frames": self._contact_sheet(job_id)}
        if tail == "thumbnail/render" and method == "POST":
            return 200, self._render(job_id, body)
        if tail == "thumbnail/approve" and method == "POST":
            return 200, self._approve(job_id, body)
        if tail == "upload-private" and method == "POST":
            return 200, self.flow.upload_private(job_id)
        if tail == "go-public" and method == "POST":
            return 200, self.flow.go_public(job_id, body.get("confirm_video_id"), body.get("confirm_text"))
        raise ApiError(404, "not_found", "Route không tồn tại.")

    # ---------- handlers ----------
    def _create_job(self, body: dict) -> dict:
        video = self.find_video(str(body.get("video_id", "")))
        if not video:
            raise ApiError(404, "not_found", "Video không còn trong thư mục nguồn.")
        return self.store.create({"video_id": video["id"], "name": video.get("name", ""),
                                  "duration": video.get("duration", 0), "rel_path": self._rel_path(video)})

    def _check_frame_file(self, job_id: str, data: dict):
        if data.get("file"):
            try:
                paths.safe_join(self._job_dir(job_id), str(data["file"]))
            except paths.PathError:
                raise ApiError(400, "invalid", "Đường dẫn frame không hợp lệ.") from None

    def _contact_sheet(self, job_id: str) -> list:
        job = self.store.get(job_id)
        out_dir = self._job_dir(job_id) / "frames"
        cache = out_dir / "contact.json"
        items = None
        if cache.is_file():
            try:
                items = json.loads(cache.read_text(encoding="utf-8"))
                if not all((out_dir / Path(i["file"]).name).is_file() for i in items):
                    items = None
            except (OSError, ValueError, KeyError, TypeError):
                items = None
        if items is None:
            raw = thumbnail.contact_sheet(self._video_path(job), out_dir, CONTACT_N)
            items = [{"index": r["index"], "time_sec": r["time_sec"], "file": f"frames/{r['file']}",
                      "thumb": f"frames/{r['thumb']}"} for r in raw]
            cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return [{**i, "url": _file_url(job_id, i["file"]), "thumb_url": _file_url(job_id, i["thumb"])} for i in items]

    def _render(self, job_id: str, body: dict) -> dict:
        job = self.store.get(job_id)
        index = body.get("frame_index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ApiError(400, "invalid", "Cần chọn frame (frame_index) trước khi tạo thumbnail.")
        job_dir = self._job_dir(job_id)
        frame = job_dir / "frames" / f"frame-{index:02}.jpg"
        if not frame.is_file():
            raise ApiError(400, "invalid", "Frame chưa được tạo; hãy mở bước chọn frame trước.")
        text, name = str(body.get("text", ""))[:200], str(body.get("template", "")).strip()[:60]
        note = str(body.get("note", ""))[:500]
        templates = thumbnail.load_templates(self.outbox / "templates.json")
        crop = (job["frame"].get("crop169") if job["frame"].get("selected_index") == index else None)
        crop = {k: crop[k] for k in _CROP_KEYS} if crop else None  # bỏ 'pos' của frontend
        with self._render_lock:
            version = thumbnail.next_version(job_dir / "thumbs")
            rel = f"thumbs/v{version}.jpg"
            result = thumbnail.render_thumbnail(frame, job_dir / rel, text, templates.get(name),
                                                body.get("safe_area") if isinstance(body.get("safe_area"), dict) else None,
                                                crop)
        entry = thumbnail.history_entry(version, rel, name, text, note)
        self.store.update(job_id, lambda j: {**j, "thumbnail": {
            **j["thumbnail"], "versions": list(j["thumbnail"]["versions"]) + [entry]}}, event="thumbnail_render",
            detail=f"v{version}")
        return {"version": version, "file": rel, "url": _file_url(job_id, rel), "warnings": result.get("warnings", [])}

    def _approve(self, job_id: str, body: dict) -> dict:
        job = self.store.get(job_id)
        version = body.get("version")
        if not any(v.get("v") == version for v in job["thumbnail"]["versions"]):
            raise ApiError(400, "invalid", "Phiên bản thumbnail không tồn tại.")
        if job["upload"]["state"] in ("in_progress", "done"):
            raise ApiError(409, "conflict", "Video đã/đang upload; không đổi thumbnail đã duyệt.")
        job = self.store.update(job_id, lambda j: {**j, "thumbnail": {**j["thumbnail"], "approved_version": version}},
                                event="thumbnail_approve", detail=f"v{version}")
        return sync_status(self.store, job)
