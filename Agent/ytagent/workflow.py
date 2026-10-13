"""Luồng nghiệp vụ nối JobStore + gates + youtube (không phụ thuộc HTTP).

API:
    ApiError(status, code, message, reasons=None, extra=None)
    sync_status(store, job) -> job          # tiến status theo gate, không bao giờ lùi
    vidiq_brief(job) -> str                 # chỉ tạo chuỗi, không gọi mạng
    UploadFlow(store, yt, client, yt_status, resolve_video, thumb_path)
        .upload_private(job_id) -> job
        .go_public(job_id, confirm_video_id, confirm_text) -> job
"""
from __future__ import annotations

import threading
import uuid

from . import gates
from .jobs import STATUSES
from .validation import validate_content

STUDIO_HINT = ("Bạn có thể tải video lên thủ công qua YouTube Studio (studio.youtube.com): "
               "chọn file gốc, dán tiêu đề/mô tả/tags đã soạn, tải thumbnail đã duyệt và đặt chế độ Riêng tư.")


class ApiError(Exception):
    def __init__(self, status, code, message, reasons=None, extra=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.reasons = list(reasons or [])
        self.extra = extra or {}

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, "reasons": self.reasons, **self.extra}


def _target_status(job: dict) -> str:
    target = "source_selected"
    if not validate_content(job.get("content")):
        target = "content_ready"
        if (job.get("vidiq") or {}).get("confirmed_by_user"):
            target = "vidiq_reviewed"
            if (job.get("thumbnail") or {}).get("approved_version") is not None:
                target = "thumbnail_approved"
    return target


def sync_status(store, job: dict) -> dict:
    """Chuyển status tiến theo gate (nội dung hợp lệ -> VidIQ xác nhận -> thumbnail duyệt)."""
    if STATUSES.index(job["status"]) >= STATUSES.index("private_uploaded"):
        return job
    target = _target_status(job)
    if STATUSES.index(target) > STATUSES.index(job["status"]):
        return store.transition(job["id"], target)
    return job


def vidiq_brief(job: dict) -> str:
    content = job.get("content") or {}
    tags = ", ".join(content.get("tags") or []) or "(chưa có)"
    desc = (content.get("description") or "")[:300] or "(chưa có)"
    return "\n".join([
        "Nhờ VidIQ rà soát video YouTube sau (làm thủ công trên app.vidiq.com):",
        f"Tên nguồn: {(job.get('source') or {}).get('name', '')}",
        f"Tiêu đề: {content.get('title') or '(chưa có)'}",
        f"Tags hiện tại: {tags}",
        f"Mô tả (300 ký tự đầu): {desc}",
        "Việc cần làm: tra từ khóa/điểm tiêu đề trên VidIQ, ghi lại số liệu THẬT bạn thấy, "
        "rồi tick xác nhận ở bước VidIQ. Ứng dụng không tự gọi VidIQ và không tạo số liệu.",
    ])


class UploadFlow:
    def __init__(self, store, yt, client, yt_status, resolve_video, thumb_path):
        self.store, self.yt, self.client = store, yt, client
        self.yt_status, self.resolve_video, self.thumb_path = yt_status, resolve_video, thumb_path
        self._busy: set = set()
        self._guard = threading.Lock()

    def _require_youtube(self):
        st = self.yt_status()
        if not (st.get("configured") and st.get("authorized")):
            raise ApiError(409, "not_configured",
                           "YouTube chưa được cấu hình/cấp quyền OAuth nên không thể upload tự động. " + STUDIO_HINT,
                           [st.get("reason", "")])

    def upload_private(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job["upload"]["state"] == "done" and job["upload"].get("youtube_video_id"):
            return job
        ok, reasons = gates.can_upload_private(job)
        if not ok:
            raise ApiError(409, "gate_failed", "Chưa đủ điều kiện upload riêng tư.", reasons)
        self._require_youtube()
        with self._guard:
            if job_id in self._busy:
                raise ApiError(409, "conflict", "Job này đang được upload, vui lòng chờ.")
            self._busy.add(job_id)
        try:
            return self._do_upload(job)
        finally:
            with self._guard:
                self._busy.discard(job_id)

    def _do_upload(self, job: dict) -> dict:
        job_id = job["id"]
        video_path = self.resolve_video(job)
        thumb = self.thumb_path(job)
        key = job["upload"].get("idempotency_key") or uuid.uuid4().hex

        def start(j):
            return {**j, "upload": {**j["upload"], "state": "in_progress", "idempotency_key": key}}
        job = self.store.update(job_id, start, event="upload_start")

        def save_id(video_id):
            self.store.update(job_id, lambda j: {**j, "upload": {**j["upload"], "youtube_video_id": video_id}},
                              event="upload_created", detail=video_id)
        try:
            result = self.yt.upload_private(job, str(video_path), str(thumb) if thumb else None,
                                            client=self.client, on_created=save_id)
        except self.yt.YouTubeError as exc:
            return self._fail(job_id, exc)
        except OSError as exc:
            return self._fail(job_id, self.yt.YouTubeError(f"Không đọc được file: {exc}", True, "invalid"))

        def done(j):
            up = {**j["upload"], "state": "done", "youtube_video_id": result["youtube_video_id"],
                  "privacy": "private", "uploaded_at": result["uploaded_at"],
                  "thumbnail_set": bool(result.get("thumbnail_set", True))}
            return {**j, "upload": up, "error": None}
        self.store.update(job_id, done, event="upload_done", detail=result["youtube_video_id"])
        return self.store.transition(job_id, "private_uploaded")

    def _fail(self, job_id: str, exc) -> "None":
        video_id = getattr(exc, "video_id", None)

        def failed(j):
            up = {**j["upload"], "state": "failed"}
            if video_id:
                up.update(youtube_video_id=video_id, thumbnail_set=False)
            return {**j, "upload": up}
        self.store.update(job_id, failed, event="upload_failed")
        job = self.store.record_error(job_id, "upload", exc.message, bool(exc.retryable))
        if exc.code in ("not_configured", "not_authorized"):
            raise ApiError(409, "not_configured", exc.message + " " + STUDIO_HINT)
        if exc.code == "in_progress":
            raise ApiError(409, "conflict", exc.message)
        raise ApiError(502, "upload_failed", exc.message, extra={"retryable": bool(exc.retryable), "job": job})

    def go_public(self, job_id: str, confirm_video_id, confirm_text) -> dict:
        job = self.store.get(job_id)
        if (job.get("public") or {}).get("state") == "done":
            return job
        ok, reasons = gates.can_go_public(job, confirm_video_id, confirm_text)
        if not ok:
            raise ApiError(409, "gate_failed", "Chưa đủ điều kiện công khai.", reasons)
        self._require_youtube()
        video_id = job["upload"]["youtube_video_id"]
        try:
            result = self.yt.set_public(video_id, client=self.client)
        except self.yt.YouTubeError as exc:
            self.store.record_error(job_id, "public", exc.message, bool(exc.retryable))
            raise ApiError(502, "public_failed", exc.message, extra={"retryable": bool(exc.retryable)})

        def done(j):
            return {**j, "upload": {**j["upload"], "privacy": "public"},
                    "public": {"confirmed_at": result["confirmed_at"], "state": "done"}, "error": None}
        self.store.update(job_id, done, event="go_public", detail=video_id)
        return self.store.transition(job_id, "public")
