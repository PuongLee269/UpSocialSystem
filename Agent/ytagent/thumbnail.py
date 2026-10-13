"""Thumbnail tooling: contact sheet, render from a real frame, mobile mockup, templates, versions.

Public API
----------
contact_sheet(video_path, out_dir, n=12) -> list[{index, time_sec, file, thumb}]
    Extract n evenly spaced real frames (full JPEG + small preview). Never modifies the video.
render_thumbnail(frame_file, out_file, text="", template=None, safe_area=None, crop169=None) -> dict
    1280x720 JPEG (<2MB) from a real frame. Returns {file, size, bytes, font, warnings, text_box}.
    crop169: optional {x,y,w,h} (fractions 0..1 or pixels) selecting a 16:9 region; else center-fit crop.
    safe_area: {margin_pct=5, ui_corner=True}; text is kept inside it and away from the
    bottom-right timestamp overlay. The frame content is never replaced or altered except crop/scale.
mockup_mobile(thumb_file, out_file) -> str
    ~168x94 thumbnail inside a phone-like frame for legibility preview.
DEFAULT_TEMPLATE, DEFAULT_SAFE_AREA, find_font(bold=True) -> (path|None, warning|None)
load_templates(path) -> dict[name, tpl]; save_template(path, name, tpl) -> dict (atomic JSON write)
    tpl = {font_size, color, stroke, position, box}; position in
    top|center|bottom|top-left|bottom-left|top-right|bottom-right|center-left|center-right.
next_version(thumbs_dir) -> int          next v number (scans v<N>.jpg)
history_entry(v, file, template, text, note="") -> {v,file,template,text,created,note}
ThumbnailError(RuntimeError): raised with a clear Vietnamese message (missing Pillow/cv2, bad input).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None
try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = ImageOps = None

THUMB_SIZE = (1280, 720)
MAX_BYTES = 2 * 1024 * 1024
PREVIEW_WIDTH = 320
DEFAULT_TEMPLATE = {"font_size": 96, "color": "#FFFFFF", "stroke": "#000000", "position": "bottom", "box": False}
DEFAULT_SAFE_AREA = {"margin_pct": 5, "ui_corner": True}
# YouTube overlays the duration timestamp in the bottom-right corner (fractions of w/h).
UI_CORNER = (0.80, 0.86)
POSITIONS = {"top", "center", "bottom", "top-left", "bottom-left", "top-right",
             "bottom-right", "center-left", "center-right"}
FONT_CANDIDATES = {
    True: ["segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf", "DejaVuSans-Bold.ttf"],
    False: ["segoeui.ttf", "arial.ttf", "tahoma.ttf", "DejaVuSans.ttf"],
}
FONT_DIRS = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
             "/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts"]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ThumbnailError(RuntimeError):
    """User-facing error (Vietnamese message)."""


def _need_pillow() -> None:
    if Image is None:
        raise ThumbnailError("Cần cài Pillow để xử lý thumbnail (pip install pillow).")


def _need_cv2() -> None:
    if cv2 is None:
        raise ThumbnailError("Cần cài opencv-python để trích frame từ video (pip install opencv-python).")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- contact sheet
def contact_sheet(video_path, out_dir, n: int = 12) -> list[dict]:
    """Save n evenly spaced frames as frame-XX.jpg plus small frame-XX-s.jpg previews."""
    _need_cv2()
    _need_pillow()
    n = max(1, min(int(n), 60))
    src = Path(video_path)
    if not src.is_file():
        raise ThumbnailError("Không tìm thấy file video nguồn.")
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise ThumbnailError("Không đọc được video. Hãy thử file MP4/H.264.")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = total / fps if total else 0
        points = [duration * (i + 0.5) / n for i in range(n)] if duration else [float(i) for i in range(n)]
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        result = []
        for i, sec in enumerate(points, 1):
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, sec) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            full = out / f"frame-{i:02}.jpg"
            small = out / f"frame-{i:02}-s.jpg"
            image.save(full, quality=92, optimize=True)
            preview = image.copy()
            preview.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH))
            preview.save(small, quality=80)
            result.append({"index": i, "time_sec": round(sec, 2), "file": full.name, "thumb": small.name})
    finally:
        cap.release()
    if not result:
        raise ThumbnailError("Không trích được frame nào từ video.")
    return result


# ---------------------------------------------------------------- fonts
def find_font(bold: bool = True) -> tuple[str | None, str | None]:
    """Return (font_path, warning). Warning set when falling back to a non-Vietnamese-safe font."""
    for name in FONT_CANDIDATES[bold]:
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p, None
    return None, "Không tìm thấy font hệ thống hỗ trợ tiếng Việt; dùng font mặc định, dấu có thể không hiển thị đúng."


def _load_font(size: int):
    path, warn = find_font(True)
    if path:
        return ImageFont.truetype(path, size), path, None
    try:
        return ImageFont.load_default(size=size), None, warn
    except TypeError:  # old Pillow
        return ImageFont.load_default(), None, warn


# ---------------------------------------------------------------- templates
def _norm_template(tpl) -> dict:
    t = dict(DEFAULT_TEMPLATE)
    t.update(tpl or {})
    try:
        t["font_size"] = max(16, min(int(t["font_size"]), 300))
    except (TypeError, ValueError):
        raise ThumbnailError("Cỡ chữ không hợp lệ.")
    for key in ("color", "stroke"):
        if not isinstance(t[key], str) or not _HEX.match(t[key]):
            raise ThumbnailError(f"Màu '{key}' phải có dạng #RRGGBB.")
    if t["position"] not in POSITIONS:
        raise ThumbnailError("Vị trí chữ không hợp lệ.")
    t["box"] = bool(t["box"])
    return t


def load_templates(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ThumbnailError(f"File template bị lỗi: {exc}")
    return data if isinstance(data, dict) else {}


def save_template(path, name: str, tpl: dict) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ThumbnailError("Tên template phải từ 1–60 ký tự.")
    clean = _norm_template(tpl)
    data = load_templates(path)
    data[name] = clean
    _atomic_write_json(Path(path), data)
    return clean


# ---------------------------------------------------------------- versions
def next_version(thumbs_dir) -> int:
    d = Path(thumbs_dir)
    best = 0
    if d.is_dir():
        for f in d.iterdir():
            m = re.fullmatch(r"v(\d+)\.jpg", f.name)
            if m:
                best = max(best, int(m.group(1)))
    return best + 1


def history_entry(v: int, file: str, template, text: str, note: str = "") -> dict:
    return {"v": int(v), "file": file, "template": template, "text": text,
            "created": _now(), "note": note}


# ---------------------------------------------------------------- render
def _crop_to_169(img, crop169):
    w, h = img.size
    if crop169:
        try:
            x, y, cw, ch = (float(crop169[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise ThumbnailError("crop169 phải có x, y, w, h.")
        if max(x, y, cw, ch) <= 1:
            x, y, cw, ch = x * w, y * h, cw * w, ch * h
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(w, int(x + cw)), min(h, int(y + ch))
        if x1 - x0 < 16 or y1 - y0 < 16:
            raise ThumbnailError("Vùng crop quá nhỏ.")
        return img.crop((x0, y0, x1, y1)).resize(THUMB_SIZE, Image.Resampling.LANCZOS)
    return ImageOps.fit(img, THUMB_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _wrap(draw, text, font, max_w, stroke_w):
    lines = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split():
            trial = (cur + " " + word).strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return [ln for ln in lines if ln]


def _place(position, box_w, box_h, area):
    left, top, right, bottom = area
    v, _, h = position.partition("-")
    h = h or "center"
    x = {"left": left, "center": (left + right - box_w) / 2, "right": right - box_w}[h]
    y = {"top": top, "center": (top + bottom - box_h) / 2, "bottom": bottom - box_h}[v]
    return int(x), int(y)


def _draw_text(img, text, tpl, safe):
    """Draw wrapped text inside the safe area; returns (font_path, warnings, bbox)."""
    W, H = img.size
    m = max(0, min(float(safe.get("margin_pct", 5)), 25)) / 100
    left, top, right, bottom = W * m, H * m, W * (1 - m), H * (1 - m)
    draw = ImageDraw.Draw(img)
    size = tpl["font_size"]
    warnings = []
    pad = 18 if tpl["box"] else 0
    while True:
        font, font_path, warn = _load_font(size)
        stroke_w = max(2, size // 14)
        lines = _wrap(draw, text, font, right - left - 2 * pad - 2 * stroke_w, stroke_w)
        line_h = int(size * 1.18)
        tw = max(draw.textlength(ln, font=font) for ln in lines) + 2 * stroke_w
        th = line_h * len(lines) + stroke_w
        if (tw + 2 * pad <= right - left and th + 2 * pad <= bottom - top) or size <= 24:
            break
        size = int(size * 0.92)
    if warn:
        warnings.append(warn)
    if size < tpl["font_size"]:
        warnings.append(f"Cỡ chữ tự giảm từ {tpl['font_size']} xuống {size} để vừa vùng an toàn.")
    bw, bh = tw + 2 * pad, th + 2 * pad
    x, y = _place(tpl["position"], bw, bh, (left, top, right, bottom))
    if safe.get("ui_corner", True):
        cx, cy = W * UI_CORNER[0], H * UI_CORNER[1]
        if x + bw > cx and y + bh > cy:  # overlaps timestamp corner -> lift above it
            y = int(cy - bh)
            if y < top:
                y = int(top)
                warnings.append("Chữ có thể chạm vùng timestamp góc dưới phải.")
    if tpl["box"]:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle((x, y, x + bw, y + bh), radius=16, fill=(0, 0, 0, 150))
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))
        draw = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        draw.text((x + pad + stroke_w, y + pad + i * line_h), ln, font=font, fill=tpl["color"],
                  stroke_width=stroke_w, stroke_fill=tpl["stroke"])
    return font_path, warnings, (x, y, x + bw, y + bh)


def _save_under_limit(img, out: Path) -> int:
    for q in (92, 88, 82, 75, 65, 55):
        img.save(out, "JPEG", quality=q, optimize=True)
        if out.stat().st_size < MAX_BYTES:
            return q
    raise ThumbnailError("Không nén được thumbnail dưới 2MB.")


def render_thumbnail(frame_file, out_file, text: str = "", template=None, safe_area=None,
                     crop169=None) -> dict:
    """Render a 1280x720 JPEG from a real frame with optional Vietnamese text overlay."""
    _need_pillow()
    src = Path(frame_file)
    if not src.is_file():
        raise ThumbnailError("Không tìm thấy frame nguồn.")
    tpl = _norm_template(template)
    safe = dict(DEFAULT_SAFE_AREA)
    safe.update(safe_area or {})
    try:
        with Image.open(src) as im:
            img = ImageOps.exif_transpose(im).convert("RGB")
    except OSError as exc:
        raise ThumbnailError(f"Không mở được frame: {exc}")
    img = _crop_to_169(img, crop169)
    font_path, warnings, bbox = None, [], None
    text = (text or "").strip()
    if text:
        font_path, warnings, bbox = _draw_text(img, text, tpl, safe)
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    _save_under_limit(img, out)
    return {"file": str(out), "size": list(THUMB_SIZE), "bytes": out.stat().st_size,
            "font": font_path, "warnings": warnings, "text_box": bbox}


# ---------------------------------------------------------------- mobile mockup
def mockup_mobile(thumb_file, out_file) -> str:
    """Tiny (~168x94) thumbnail inside a phone frame to check legibility at feed size."""
    _need_pillow()
    src = Path(thumb_file)
    if not src.is_file():
        raise ThumbnailError("Không tìm thấy thumbnail.")
    with Image.open(src) as im:
        small = im.convert("RGB").resize((168, 94), Image.Resampling.LANCZOS)
    w, h = 240, 200
    canvas = Image.new("RGB", (w, h), "#e9ecef")
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((18, 6, w - 18, h + 20), radius=26, fill="#111111")
    d.rounded_rectangle((26, 22, w - 26, h + 20), radius=14, fill="#ffffff")
    d.rounded_rectangle((w // 2 - 24, 11, w // 2 + 24, 16), radius=3, fill="#333333")
    canvas.paste(small, ((w - 168) // 2, 34))
    d.rectangle((36, 134, 150, 142), fill="#cccccc")
    d.rectangle((36, 150, 110, 156), fill="#e1e1e1")
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG" if out.suffix.lower() == ".png" else "JPEG")
    return str(out)
