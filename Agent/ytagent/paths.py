"""Đường dẫn an toàn.

API:
    class PathError(ValueError)
    safe_join(root: str|Path, rel: str) -> Path
        Nối rel vào root; ném PathError nếu rel rỗng/tuyệt đối/có '..'/NUL
        hoặc kết quả (sau resolve, kể cả symlink) thoát khỏi root.
    resolve_in_roots(roots: Sequence[str|Path], rel: str) -> Path
        rel dạng "<root_index>/<đường dẫn>" -> safe_join(roots[index], ...).
    is_secret_path(rel: str) -> bool
        True nếu đường dẫn chạm secrets/, token, client_secret (không phục vụ qua /api/file).
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

SECRET_MARKERS = ("secrets", "token", "client_secret")


class PathError(ValueError):
    """Đường dẫn không hợp lệ hoặc thoát khỏi thư mục gốc."""


def _clean_parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise PathError("Đường dẫn rỗng hoặc không hợp lệ.")
    text = rel.replace("\\", "/")
    if text.startswith("/") or PureWindowsPath(rel).drive or PurePosixPath(text).is_absolute():
        raise PathError("Không chấp nhận đường dẫn tuyệt đối.")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PathError("Đường dẫn chứa '..' hoặc rỗng.")
    return parts


def safe_join(root, rel: str) -> Path:
    base = Path(root).resolve()
    target = base.joinpath(*_clean_parts(rel)).resolve()
    if target != base and base not in target.parents:
        raise PathError("Đường dẫn thoát khỏi thư mục gốc.")
    return target


def resolve_in_roots(roots, rel: str) -> Path:
    parts = _clean_parts(rel)
    if len(parts) < 2 or not parts[0].isdigit():
        raise PathError("Đường dẫn phải có dạng <chỉ_số_root>/<đường_dẫn>.")
    index = int(parts[0])
    if index >= len(roots):
        raise PathError("Chỉ số thư mục gốc không tồn tại.")
    return safe_join(roots[index], "/".join(parts[1:]))


def is_secret_path(rel: str) -> bool:
    lowered = str(rel).replace("\\", "/").lower()
    return any(marker in lowered for marker in SECRET_MARKERS)
