"""Art Director: tạo tối đa 3 phương án thumbnail AI từ brief + metadata job.

Tiết kiệm token: đúng MỘT lần gọi LLM local (Ollama). Không có LLM -> template cố định, ghi rõ
"template, không phải AI". Không gọi mạng ngoài máy; hàm `generate` do bên gọi truyền vào.

API:
    make_concepts(job, brief, generate=None, has_style_ref=False) -> {concepts, source, engine, note}
        generate(prompt)->(text|None, engine_label)   # ví dụ app.ollama_generate
    parse_concepts(text) -> list[dict]                  # lọc/chuẩn hóa; [] nếu không đọc được
    template_concepts(job, brief) -> list[dict]
Mỗi concept: {style_name, prompt, negative, mode, denoise, text_overlay, font_preset}
"""
from __future__ import annotations

import json

MAX_CONCEPTS = 3
MODES = ("img2img", "inpaint_bg")
DEFAULT_NEGATIVE = ("text, letters, watermark, logo, caption, deformed face, extra fingers, blurry, "
                    "low quality, celebrity likeness")
NO_TEXT_NOTE = "no text, no letters, no watermark"
TEMPLATE_NOTE = "Phương án từ template cố định, không phải AI (chưa có Ollama local)."

_TEMPLATES = (
    ("Điện ảnh ấm", "cinematic warm color grade, soft rim light, shallow depth of field, high contrast, {subject}",
     "img2img", 0.45, "Impact"),
    ("Nền mới nổi bật", "same subject, dramatic new background, vivid saturated colors, clean composition, {subject}",
     "inpaint_bg", 0.85, "Anton"),
    ("Tối giản sáng", "bright minimalist look, clean background, soft daylight, subject centered, {subject}",
     "img2img", 0.6, "Be Vietnam Bold"),
)


def _clip(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _subject(job: dict, brief: str) -> str:
    content = job.get("content") or {}
    return _clip(brief or content.get("title") or (job.get("source") or {}).get("name"), 200)


def _num(value, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(min(hi, max(lo, number)), 2)


def _clean_concept(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    prompt = _clip(raw.get("prompt"), 800)
    if not prompt:
        return None
    mode = raw.get("mode") if raw.get("mode") in MODES else "img2img"
    if NO_TEXT_NOTE.split(",")[0] not in prompt.lower():
        prompt = f"{prompt}, {NO_TEXT_NOTE}"[:900]
    return {"style_name": _clip(raw.get("style_name"), 60) or "Phương án",
            "prompt": prompt,
            "negative": _clip(raw.get("negative"), 400) or DEFAULT_NEGATIVE,
            "mode": mode,
            "denoise": _num(raw.get("denoise"), 0.85 if mode == "inpaint_bg" else 0.55, 0.05, 1.0),
            "text_overlay": _clip(raw.get("text_overlay"), 60),
            "font_preset": _clip(raw.get("font_preset"), 40)}


def parse_concepts(text) -> list[dict]:
    if not isinstance(text, str):
        return []
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    items = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    cleaned = (_clean_concept(i) for i in items)
    return [c for c in cleaned if c][:MAX_CONCEPTS]


def template_concepts(job: dict, brief: str) -> list[dict]:
    subject = _subject(job, brief)
    title = _clip((job.get("content") or {}).get("title"), 60)
    out = []
    for name, tpl, mode, denoise, font in _TEMPLATES:
        out.append(_clean_concept({"style_name": name, "prompt": tpl.format(subject=subject), "mode": mode,
                                   "denoise": denoise, "text_overlay": title, "font_preset": font}))
    return out


def build_prompt(job: dict, brief: str, has_style_ref: bool = False) -> str:
    content = job.get("content") or {}
    info = {"title": _clip(content.get("title"), 100), "tags": [_clip(t, 40) for t in (content.get("tags") or [])[:10]],
            "description": _clip(content.get("description"), 300), "brief": _clip(brief, 500)}
    style = " The user attached a style reference image (you cannot see it); keep styles generic." if has_style_ref else ""
    return (
        "You are an art director for YouTube thumbnails. Reply with JSON ONLY, no markdown: "
        '{"concepts":[{"style_name":"","prompt":"","negative":"","mode":"img2img|inpaint_bg",'
        '"denoise":0.1-1.0,"text_overlay":"","font_preset":""}]} with 3 concepts max. '
        "Rules: prompt and negative in English, for an image-to-image model that edits a REAL video frame; "
        "never ask the model to draw text/letters/logos; no real celebrities; no misleading content. "
        "text_overlay is a short Vietnamese headline (max 40 chars, drawn later by the app, not by the model). "
        "mode inpaint_bg keeps the subject and repaints only the background; img2img restyles the whole frame."
        f"{style}\nVideo info: {json.dumps(info, ensure_ascii=False)}")


def make_concepts(job: dict, brief: str, generate=None, has_style_ref: bool = False) -> dict:
    if generate is not None:
        try:
            text, engine = generate(build_prompt(job, brief, has_style_ref))
        except Exception as exc:  # LLM local lỗi không được làm hỏng luồng
            text, engine = None, f"Lỗi gọi LLM local ({type(exc).__name__})."
        concepts = parse_concepts(text)
        if concepts:
            return {"concepts": concepts, "source": "ollama", "engine": engine,
                    "note": f"Phương án do AI local soạn ({engine}). Hãy đọc lại trước khi tạo ảnh."}
        reason = engine if not text else "LLM local trả về định dạng không đọc được."
        return {"concepts": template_concepts(job, brief), "source": "template", "engine": "template",
                "note": f"{TEMPLATE_NOTE} Lý do: {reason}"}
    return {"concepts": template_concepts(job, brief), "source": "template", "engine": "template", "note": TEMPLATE_NOTE}
