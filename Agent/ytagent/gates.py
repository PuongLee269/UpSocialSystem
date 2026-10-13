"""Cổng kiểm soát upload/công khai. Trả (ok: bool, reasons: list[str]).

API:
    CONFIRM_TEXT = "CONG KHAI"
    CHECKLIST_KEYS = ("rights","policy","metadata","disclosure","kids")
    can_upload_private(job: dict) -> (bool, list[str])
    can_go_public(job: dict, confirm_video_id: str|None, confirm_text: str|None) -> (bool, list[str])
"""
from __future__ import annotations

from .validation import validate_content

CONFIRM_TEXT = "CONG KHAI"
CHECKLIST_KEYS = ("rights", "policy", "metadata", "disclosure", "kids")


def can_upload_private(job: dict) -> tuple[bool, list[str]]:
    reasons = list(validate_content(job.get("content")))
    if (job.get("frame") or {}).get("selected_index") is None:
        reasons.append("Chưa chọn frame.")
    if not (job.get("vidiq") or {}).get("confirmed_by_user"):
        reasons.append("Bạn chưa xác nhận đã xem VidIQ.")
    if (job.get("thumbnail") or {}).get("approved_version") is None:
        reasons.append("Chưa duyệt thumbnail.")
    checklist = job.get("checklist") or {}
    for key in CHECKLIST_KEYS:
        if checklist.get(key) is not True:
            reasons.append(f"Checklist chưa đạt: {key}.")
    return not reasons, reasons


def can_go_public(job: dict, confirm_video_id=None, confirm_text=None) -> tuple[bool, list[str]]:
    reasons = []
    upload = job.get("upload") or {}
    video_id = upload.get("youtube_video_id")
    if job.get("status") != "private_uploaded":
        reasons.append("Video chưa ở trạng thái đã upload riêng tư.")
    if upload.get("state") != "done":
        reasons.append("Upload riêng tư chưa hoàn tất.")
    if not video_id:
        reasons.append("Chưa có YouTube video ID.")
    reasons += can_upload_private(job)[1]
    if not video_id or confirm_video_id != video_id:
        reasons.append("confirm_video_id không khớp YouTube video ID.")
    if confirm_text != CONFIRM_TEXT:
        reasons.append(f"Cần nhập chính xác '{CONFIRM_TEXT}' để xác nhận.")
    return not reasons, reasons
