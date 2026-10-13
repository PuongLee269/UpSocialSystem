"""Kiểm tra metadata theo giới hạn YouTube. Mỗi hàm trả list[str] lỗi (rỗng = hợp lệ).

API:
    validate_title(title: str) -> list[str]        # 1-100 ký tự, không '<' '>'
    validate_description(text: str) -> list[str]   # <= 5000 byte UTF-8, không '<' '>'
    validate_tags(tags: list[str]) -> list[str]    # mỗi tag <=100, tổng <=500, không trùng/rỗng
    tags_total_chars(tags: list[str]) -> int       # tag có dấu cách +2 (nháy)
    validate_content(content: dict) -> list[str]   # gộp 3 hàm trên
"""
from __future__ import annotations

TITLE_MAX = 100
DESC_MAX_BYTES = 5000
TAG_MAX = 100
TAGS_TOTAL_MAX = 500


def validate_title(title) -> list[str]:
    if not isinstance(title, str) or not title.strip():
        return ["Tiêu đề không được để trống."]
    errors = []
    if len(title) > TITLE_MAX:
        errors.append(f"Tiêu đề dài {len(title)} ký tự, tối đa {TITLE_MAX}.")
    if "<" in title or ">" in title:
        errors.append("Tiêu đề không được chứa '<' hoặc '>'.")
    return errors


def validate_description(text) -> list[str]:
    if not isinstance(text, str):
        return ["Mô tả phải là văn bản."]
    errors = []
    size = len(text.encode("utf-8"))
    if size > DESC_MAX_BYTES:
        errors.append(f"Mô tả dài {size} byte, tối đa {DESC_MAX_BYTES} byte.")
    if "<" in text or ">" in text:
        errors.append("Mô tả không được chứa '<' hoặc '>'.")
    return errors


def tags_total_chars(tags) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags if isinstance(t, str))


def validate_tags(tags) -> list[str]:
    if not isinstance(tags, (list, tuple)):
        return ["Tags phải là danh sách."]
    errors, seen = [], set()
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            errors.append("Có tag rỗng.")
            continue
        if len(tag) > TAG_MAX:
            errors.append(f"Tag '{tag[:20]}...' dài {len(tag)} ký tự, tối đa {TAG_MAX}.")
        key = tag.strip().lower()
        if key in seen:
            errors.append(f"Tag '{tag}' bị trùng.")
        seen.add(key)
    total = tags_total_chars(tags)
    if total > TAGS_TOTAL_MAX:
        errors.append(f"Tổng độ dài tags {total} ký tự, tối đa {TAGS_TOTAL_MAX}.")
    return errors


def validate_content(content) -> list[str]:
    content = content or {}
    return (validate_title(content.get("title")) + validate_description(content.get("description", ""))
            + validate_tags(content.get("tags", [])))
