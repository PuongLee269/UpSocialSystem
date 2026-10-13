"""JobStore: job v2 (schema_version=2) lưu ở <outbox>/jobs/<id>/job.json.

API:
    class JobError(Exception); NotFoundError, InvalidError, ConflictError (409)
    STATUSES = (source_selected, content_ready, vidiq_reviewed, thumbnail_approved, private_uploaded, public)
    SECTIONS = ("frame","content","vidiq","checklist")
    JobStore(outbox_path)
      .create(source: dict) -> job            # source={video_id,name,duration,rel_path}; idempotent theo video_id (job chưa public)
      .get(job_id) -> job                     # NotFoundError nếu không có
      .list() -> list[{id,status,title,updated}]   # mới nhất trước
      .update_section(job_id, section, data, expected_revision) -> job   # merge nông; ConflictError nếu lệch revision
      .update(job_id, fn, expected_revision=None, event="", detail="") -> job
            # fn(copy_của_job) -> job mới (cho upload/thumbnail/public); không được đổi id/schema_version
      .transition(job_id, new_status, expected_revision=None, detail="") -> job   # chỉ tiến, không lùi
      .record_error(job_id, stage, message, retryable=True) -> job   # không đổi status
      .import_legacy(legacy_id) -> job        # đọc <outbox>/<legacy_id>/review.json, không sửa file cũ; idempotent
Mọi job trả về là bản sao sâu (bất biến với phía gọi). Ghi atomic (tmp + os.replace), lock theo job.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .validation import tags_total_chars

SCHEMA_VERSION = 2
STATUSES = ("source_selected", "content_ready", "vidiq_reviewed",
            "thumbnail_approved", "private_uploaded", "public")
SECTION_KEYS = {
    "frame": {"selected_index", "time_sec", "file", "crop169"},
    "content": {"title", "description", "tags", "tags_total_chars", "made_for_kids"},
    "vidiq": {"reviewed", "reviewed_at", "lookup_date", "keywords", "thumbnail_score",
              "notes", "confirmed_by_user"},
    "checklist": {"rights", "policy", "metadata", "disclosure", "kids", "ai_flags"},
}
SECTIONS = tuple(SECTION_KEYS)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_PROTECTED = ("id", "schema_version")


class JobError(Exception):
    """Lỗi gốc của JobStore."""


class NotFoundError(JobError):
    pass


class InvalidError(JobError):
    pass


class ConflictError(JobError):
    """Kiểu 409: revision lệch hoặc thao tác xung đột."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_job(source: dict) -> dict:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION, "id": uuid.uuid4().hex, "created": now, "updated": now,
        "revision": 1, "status": "source_selected", "error": None,
        "source": {"video_id": source.get("video_id", ""), "name": source.get("name", ""),
                   "duration": source.get("duration", 0), "rel_path": source.get("rel_path", "")},
        "frame": {"selected_index": None, "time_sec": None, "file": None, "crop169": None},
        "content": {"title": "", "description": "", "tags": [], "tags_total_chars": 0, "made_for_kids": False},
        "vidiq": {"reviewed": False, "reviewed_at": None, "lookup_date": None, "keywords": [],
                  "thumbnail_score": {"value": None, "score_for": "thumbnail", "file": None, "version": None},
                  "notes": "", "confirmed_by_user": False},
        "thumbnail": {"versions": [], "approved_version": None, "template": {}},
        "checklist": {"rights": False, "policy": False, "metadata": False,
                      "disclosure": False, "kids": False, "ai_flags": []},
        "upload": {"state": "none", "idempotency_key": "", "youtube_video_id": None,
                   "privacy": None, "uploaded_at": None},
        "public": {"confirmed_at": None, "state": "locked"},
        "platforms": {"youtube": {}, "tiktok": None, "instagram": None, "facebook": None},
        "history": [{"at": now, "event": "created", "detail": ""}],
    }


class JobStore:
    def __init__(self, outbox_path):
        self.outbox = Path(outbox_path)
        self.jobs_dir = self.outbox / "jobs"
        self._guard = threading.Lock()
        self._locks: dict = {}

    # --- nội bộ ---
    def _lock(self, key: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    def _path(self, job_id) -> Path:
        if not isinstance(job_id, str) or not _ID_RE.match(job_id):
            raise NotFoundError("Không tìm thấy job.")
        return self.jobs_dir / job_id / "job.json"

    def _read(self, job_id) -> dict:
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFoundError("Không tìm thấy job.") from None
        except (json.JSONDecodeError, OSError) as exc:
            raise JobError(f"Không đọc được job: {exc}") from exc

    def _write(self, job: dict) -> dict:
        path = self._path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"job.json.tmp-{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return copy.deepcopy(job)

    def _mutate(self, job_id, fn, expected_revision=None, event="", detail=""):
        with self._lock(str(job_id)):
            current = self._read(job_id)
            if expected_revision is not None and current.get("revision") != expected_revision:
                raise ConflictError("Job đã được thay đổi ở nơi khác (revision lệch). Hãy tải lại.")
            new = fn(copy.deepcopy(current))
            for key in _PROTECTED:
                if new.get(key) != current.get(key):
                    raise InvalidError(f"Không được đổi trường '{key}'.")
            now = _now()
            new = {**new, "revision": current["revision"] + 1, "updated": now}
            if event:
                new["history"] = list(new.get("history", [])) + [{"at": now, "event": event, "detail": detail}]
            return self._write(new)

    def _all(self) -> list:
        jobs = []
        paths = self.jobs_dir.glob("*/job.json") if self.jobs_dir.exists() else []
        for path in paths:
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(jobs, key=lambda j: j.get("updated", ""), reverse=True)

    # --- API ---
    def create(self, source: dict) -> dict:
        video_id = (source or {}).get("video_id")
        if not video_id:
            raise InvalidError("Thiếu video_id.")
        with self._lock("__create__"):
            for item in self._all():
                if item["source"].get("video_id") == video_id and item["status"] != "public":
                    return item
            return self._write(_new_job(source))

    def get(self, job_id) -> dict:
        with self._lock(str(job_id)):
            return self._read(job_id)

    def list(self) -> list:
        return [{"id": j["id"], "status": j["status"], "title": j.get("content", {}).get("title", ""),
                 "updated": j.get("updated")} for j in self._all()]

    def update_section(self, job_id, section, data, expected_revision) -> dict:
        if section not in SECTION_KEYS:
            raise InvalidError(f"Section không hợp lệ: {section}.")
        if not isinstance(data, dict):
            raise InvalidError("Dữ liệu section phải là object.")
        unknown = set(data) - SECTION_KEYS[section]
        if unknown:
            raise InvalidError(f"Trường không hợp lệ: {', '.join(sorted(unknown))}.")
        if expected_revision is None:
            raise InvalidError("Thiếu expected_revision.")

        def apply(job):
            merged = {**job[section], **copy.deepcopy(data)}
            if section == "content" and isinstance(merged.get("tags"), list):
                merged["tags_total_chars"] = tags_total_chars(merged["tags"])
            return {**job, section: merged}
        return self._mutate(job_id, apply, expected_revision, f"update_{section}")

    def update(self, job_id, fn, expected_revision=None, event="", detail="") -> dict:
        return self._mutate(job_id, fn, expected_revision, event, detail)

    def transition(self, job_id, new_status, expected_revision=None, detail="") -> dict:
        if new_status not in STATUSES:
            raise InvalidError(f"Trạng thái không hợp lệ: {new_status}.")

        def apply(job):
            if STATUSES.index(new_status) <= STATUSES.index(job["status"]):
                raise ConflictError(f"Không thể chuyển từ '{job['status']}' sang '{new_status}'.")
            return {**job, "status": new_status}
        return self._mutate(job_id, apply, expected_revision, f"status:{new_status}", detail)

    def record_error(self, job_id, stage, message, retryable=True) -> dict:
        err = {"stage": stage, "message": message, "at": _now(), "retryable": bool(retryable)}
        return self._mutate(job_id, lambda job: {**job, "error": err}, None, "error", f"{stage}: {message}")

    def import_legacy(self, legacy_id) -> dict:
        if not isinstance(legacy_id, str) or not _ID_RE.match(legacy_id):
            raise NotFoundError("Không tìm thấy job cũ.")
        path = self.outbox / legacy_id / "review.json"
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFoundError("Không tìm thấy job cũ.") from None
        except (json.JSONDecodeError, OSError) as exc:
            raise InvalidError(f"review.json không đọc được: {exc}") from exc
        with self._lock("__create__"):
            for item in self._all():
                if item.get("legacy_id") == legacy_id:
                    return item
            video = legacy.get("video") or {}
            job = _new_job({"video_id": video.get("id", ""), "name": video.get("name", ""),
                            "duration": video.get("duration", 0), "rel_path": ""})
            yt = (legacy.get("platforms") or {}).get("youtube") or {}
            raw_tags = yt.get("tags") if isinstance(yt.get("tags"), list) else []
            tags = [t for t in raw_tags if isinstance(t, str)]
            job["content"] = {"title": str(yt.get("title", ""))[:100], "description": str(yt.get("description", "")),
                              "tags": tags, "tags_total_chars": tags_total_chars(tags)}
            job["legacy_id"] = legacy_id
            job["history"].append({"at": _now(), "event": "import_legacy", "detail": legacy_id})
            return self._write(job)
