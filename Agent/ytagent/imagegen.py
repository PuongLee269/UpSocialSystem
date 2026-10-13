"""Tao anh AI local qua ComfyUI HTTP API (chi stdlib + Pillow cho inpaint_bg).

Public API
----------
status(cfg) -> {configured, reachable, reason}
    Trung thuc: chua cau hinh url / ComfyUI chua chay -> noi ro, khong gia.
list_workflows(workflows_dir) -> [{name, file, placeholders, model}]
generate(cfg, workflow_name, frame_file, prompt, negative="", mode="img2img",
         denoise=0.5, seed=None, mask_file=None, out_dir=".", timeout=300)
    -> {file, meta_file, seed, workflow, prompt, mode, model, ai_generated: True, warnings}
    mode: img2img | inpaint_bg | txt2img.
    mask_file (inpaint_bg): anh trang/den, TRANG = vung GIU NGUYEN (nguoi/mat).
    Sau khi sinh, pixel goc trong vung trang duoc ghep lai nen khuon mat khong doi.
    Workflow nhan mask DA DAO (trang = vung duoc phep sinh = nen) qua {{mask}}.
    ai_generated luon True de UI ghi nhan.
Placeholder trong workflow API-format: {{prompt}} {{negative}} {{image}} {{mask}}
    {{seed}} {{denoise}} {{width}} {{height}}.
cfg: comfyui_url, comfyui_workflow (ten mac dinh), comfyui_workflows_dir (tuy chon).
ImageGenError(RuntimeError): thong bao tieng Viet.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageOps = None

MODES = ("img2img", "inpaint_bg", "txt2img")
_PH = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_NAME_OK = re.compile(r"^[\w.\- ]+$")
MODEL_KEYS = ("ckpt_name", "unet_name", "model_name")
DEFAULT_SIZE = (1280, 720)
MAX_SIDE = 2048
POLL_INTERVAL = 0.3


class ImageGenError(RuntimeError):
    """Loi nguoi dung doc duoc (tieng Viet)."""


class _HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}")
        self.code, self.body = code, body


# ---------------------------------------------------------------- HTTP
def _base(cfg) -> str:
    return str((cfg or {}).get("comfyui_url") or "").strip().rstrip("/")


def _request(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise _HttpError(e.code, e.read().decode("utf-8", "replace")) from None
    except (urllib.error.URLError, OSError) as e:
        raise ImageGenError(f"Không kết nối được ComfyUI ({url.split('?')[0]}): {e}. "
                            "Hãy chạy ComfyUI rồi thử lại.") from None


def _json(url, payload=None, timeout=30):
    data = headers = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    try:
        raw = _request(url, data, headers, timeout)
    except _HttpError as e:
        raise ImageGenError(_explain_http_error(e)) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ImageGenError("ComfyUI trả về dữ liệu không phải JSON; kiểm tra comfyui_url.") from None


def _explain_http_error(e: _HttpError) -> str:
    try:
        info = json.loads(e.body)
    except ValueError:
        return f"ComfyUI báo lỗi HTTP {e.code}: {e.body[:200]}"
    msgs = []
    err = info.get("error")
    if isinstance(err, dict):
        msgs.append(str(err.get("message") or err.get("type") or ""))
    node_errors = info.get("node_errors") or {}
    for nid, ne in node_errors.items():
        for item in ne.get("errors", []):
            et, det = item.get("type", ""), item.get("extra_info", {}) or {}
            if et == "value_not_in_list":
                msgs.append(f"Thiếu model hoặc giá trị không có trong ComfyUI (node {nid}, "
                            f"{det.get('input_name', '?')}={det.get('received_value', '?')}). "
                            "Hãy đặt model vào thư mục models của ComfyUI hoặc sửa workflow.")
            else:
                msgs.append(f"node {nid} ({ne.get('class_type', '?')}): {item.get('message', et)}")
    if e.code == 400 and not node_errors:
        msgs.append("Workflow sai định dạng (cần xuất bằng 'Save (API Format)').")
    return "Workflow bị ComfyUI từ chối: " + "; ".join(m for m in msgs if m)


def _multipart(fields, file_field, filename, content, mime):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode())
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def _upload(base, path: Path, timeout) -> str:
    suffix = path.suffix.lower() or ".png"
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    body, headers = _multipart({"overwrite": "true", "type": "input"}, "image",
                               f"ytagent_{uuid.uuid4().hex[:8]}{suffix}", path.read_bytes(), mime)
    try:
        raw = _request(base + "/upload/image", body, headers, timeout)
    except _HttpError as e:
        raise ImageGenError(f"Upload ảnh lên ComfyUI thất bại: {e.body[:200]}") from None
    try:
        info = json.loads(raw.decode("utf-8"))
        sub = info.get("subfolder") or ""
        return f"{sub}/{info['name']}" if sub else info["name"]
    except (ValueError, KeyError):
        raise ImageGenError("ComfyUI trả về phản hồi upload không hợp lệ.") from None


# ---------------------------------------------------------------- status / workflows
def status(cfg) -> dict:
    base = _base(cfg)
    if not base:
        return {"configured": False, "reachable": False,
                "reason": "Chưa cấu hình comfyui_url trong config.json; tính năng ảnh AI đang tắt."}
    try:
        json.loads(_request(base + "/system_stats", timeout=3).decode("utf-8"))
    except ImageGenError as e:
        return {"configured": True, "reachable": False,
                "reason": f"ComfyUI chưa chạy hoặc không truy cập được: {e}"}
    except (_HttpError, ValueError):
        return {"configured": True, "reachable": False,
                "reason": f"{base} có phản hồi nhưng không giống ComfyUI (kiểm tra URL/cổng)."}
    return {"configured": True, "reachable": True, "reason": "ComfyUI sẵn sàng."}


def _walk(node, fn):
    if isinstance(node, dict):
        return {k: _walk(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, fn) for v in node]
    return fn(node)


def _placeholders_in(wf) -> set:
    found = set()

    def grab(v):
        if isinstance(v, str):
            found.update(_PH.findall(v))
        return v
    _walk(wf, grab)
    return found


def _model_of(wf):
    for node in (wf or {}).values():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        for key in MODEL_KEYS:
            val = inputs.get(key)
            if isinstance(val, str) and not _PH.search(val):
                return val
    return None


def _safe_wf_path(workflows_dir, name) -> Path:
    stem = str(name or "")
    if stem.lower().endswith(".json"):
        stem = stem[:-5]
    if not stem or not _NAME_OK.match(stem) or ".." in stem:
        raise ImageGenError(f"Tên workflow không hợp lệ: {name!r}.")
    p = Path(workflows_dir) / f"{stem}.json"
    if not p.is_file():
        raise ImageGenError(f"Không tìm thấy workflow '{stem}' trong {workflows_dir}.")
    return p


def _load_workflow(path: Path) -> dict:
    try:
        wf = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ImageGenError(f"Workflow '{path.name}' không đọc được hoặc không phải JSON: {e}") from None
    ok = isinstance(wf, dict) and wf and all(isinstance(n, dict) and "class_type" in n for n in wf.values())
    if not ok:
        raise ImageGenError(f"Workflow '{path.name}' sai định dạng: cần xuất bằng 'Save (API Format)' "
                            "(mỗi node có class_type), không phải workflow giao diện thường.")
    return wf


def list_workflows(workflows_dir) -> list:
    d = Path(workflows_dir)
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            wf = _load_workflow(p)
        except ImageGenError:
            continue
        out.append({"name": p.stem, "file": p.name,
                    "placeholders": sorted(_placeholders_in(wf)), "model": _model_of(wf)})
    return out


# ---------------------------------------------------------------- generate
def _substitute(wf, values: dict):
    def sub(v):
        if not isinstance(v, str):
            return v
        m = _PH.fullmatch(v.strip())
        if m and m.group(1) in values:
            return values[m.group(1)]  # giu nguyen kieu int/float/str
        return _PH.sub(lambda mm: str(values.get(mm.group(1), mm.group(0))), v)
    return _walk(wf, sub)


def _fit_size(path):
    if path is None or Image is None:
        return DEFAULT_SIZE
    with Image.open(path) as im:
        w, h = im.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    return max(8, int(w * scale) // 8 * 8), max(8, int(h * scale) // 8 * 8)


def _find_output(entry: dict):
    outputs = (entry.get("outputs") or {}).values()
    for out in outputs:
        for img in out.get("images", []) or []:
            if img.get("type", "output") == "output":
                return img
    for out in outputs:
        if out.get("images"):
            return out["images"][0]
    return None


def _wait_history(base, prompt_id, timeout) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        entry = _json(f"{base}/history/{urllib.parse.quote(prompt_id)}", timeout=15).get(prompt_id)
        if entry:
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                msg = ""
                for ev in st.get("messages", []):
                    if ev and ev[0] == "execution_error":
                        msg = f"{ev[1].get('node_type', '?')}: {ev[1].get('exception_message', '')}".strip()
                raise ImageGenError(f"ComfyUI lỗi khi chạy hàng đợi: {msg or 'không rõ nguyên nhân'}")
            if entry.get("outputs"):
                return entry
        if time.monotonic() >= deadline:
            raise ImageGenError(f"Quá thời gian chờ ({timeout}s) mà ComfyUI chưa sinh xong ảnh. "
                                "Thử tăng timeout hoặc dùng model nhẹ hơn.")
        time.sleep(POLL_INTERVAL)


def _paste_original(orig_path, gen_bytes, mask_path, out_path):
    """Ghep lai pixel goc o vung mask trang (giu nguyen nguoi/mat)."""
    with Image.open(orig_path) as o:
        orig = ImageOps.exif_transpose(o).convert("RGB")
    gen = Image.open(io.BytesIO(gen_bytes)).convert("RGB").resize(orig.size, Image.LANCZOS)
    with Image.open(mask_path) as m:
        mask = m.convert("L").resize(orig.size, Image.LANCZOS)
    Image.composite(orig, gen, mask).save(out_path, "PNG")


def _validate_inputs(mode, frame, mask, prompt):
    if mode not in MODES:
        raise ImageGenError(f"Chế độ không hợp lệ: {mode!r} (chọn img2img, inpaint_bg hoặc txt2img).")
    if not str(prompt or "").strip():
        raise ImageGenError("Cần nhập prompt mô tả ảnh muốn tạo.")
    if mode != "txt2img" and not (frame and Path(frame).is_file()):
        raise ImageGenError("Chế độ này cần ảnh khung hình gốc (frame_file) tồn tại.")
    if mode == "inpaint_bg":
        if Image is None:
            raise ImageGenError("Thiếu Pillow: chế độ inpaint_bg cần Pillow để ghép lại vùng người. "
                                "Cài bằng: pip install pillow")
        if not (mask and Path(mask).is_file()):
            raise ImageGenError("inpaint_bg cần mask_file (trắng = vùng người/mặt giữ nguyên).")


def _check_workflow_fit(mode, need):
    warnings = []
    if mode == "txt2img" and "image" in need:
        raise ImageGenError("Workflow này cần ảnh đầu vào ({{image}}) nên không chạy được chế độ txt2img.")
    if mode == "inpaint_bg" and "mask" not in need:
        raise ImageGenError("Workflow không có {{mask}} nên không dùng được cho inpaint_bg.")
    if mode != "txt2img" and "image" not in need:
        warnings.append("Workflow không có {{image}}: ảnh gốc bị bỏ qua (kết quả giống txt2img).")
    if mode == "img2img" and "denoise" not in need:
        warnings.append("Workflow không có {{denoise}}: độ biến đổi do workflow tự quyết định.")
    return warnings


def _prepare_inputs(base, mode, frame_file, mask_file, out_dir, timeout, values):
    if mode != "txt2img":
        values["image"] = _upload(base, Path(frame_file), timeout)
    if mode == "inpaint_bg":
        with Image.open(mask_file) as m:
            inv = ImageOps.invert(m.convert("L"))
        tmp = out_dir / f".mask-{uuid.uuid4().hex[:8]}.png"
        inv.save(tmp, "PNG")
        try:
            values["mask"] = _upload(base, tmp, timeout)
        finally:
            tmp.unlink(missing_ok=True)


def generate(cfg, workflow_name, frame_file, prompt, negative="", mode="img2img", denoise=0.5,
             seed=None, mask_file=None, out_dir=".", timeout=300) -> dict:
    _validate_inputs(mode, frame_file, mask_file, prompt)
    base = _base(cfg)
    if not base:
        raise ImageGenError("Chưa cấu hình comfyui_url trong config.json.")
    wf_dir = (cfg or {}).get("comfyui_workflows_dir") or Path(__file__).resolve().parent.parent / "workflows"
    wf_path = _safe_wf_path(wf_dir, workflow_name or (cfg or {}).get("comfyui_workflow"))
    wf = _load_workflow(wf_path)
    warnings = _check_workflow_fit(mode, _placeholders_in(wf))
    try:
        denoise = float(denoise)
    except (TypeError, ValueError):
        raise ImageGenError("denoise phải là số trong khoảng 0..1.") from None
    if not 0.0 <= denoise <= 1.0:
        raise ImageGenError("denoise phải nằm trong khoảng 0..1.")
    seed = int(seed) if seed is not None else int.from_bytes(os.urandom(6), "big")

    started = time.monotonic()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = _fit_size(Path(frame_file) if frame_file else None)
    values = {"prompt": str(prompt), "negative": str(negative or ""), "seed": seed,
              "denoise": denoise, "width": w, "height": h}
    _prepare_inputs(base, mode, frame_file, mask_file, out_dir, timeout, values)

    resp = _json(base + "/prompt", {"prompt": _substitute(wf, values), "client_id": uuid.uuid4().hex})
    pid = resp.get("prompt_id")
    if not pid:
        raise ImageGenError("ComfyUI không nhận job (không có prompt_id); kiểm tra hàng đợi/workflow.")
    img = _find_output(_wait_history(base, pid, timeout))
    if not img:
        raise ImageGenError("Workflow chạy xong nhưng không có node lưu ảnh (SaveImage/PreviewImage).")
    q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    try:
        gen_bytes = _request(f"{base}/view?{q}", timeout=60)
    except _HttpError as e:
        raise ImageGenError(f"Không tải được ảnh kết quả từ ComfyUI: HTTP {e.code}") from None

    out_file = out_dir / f"ai-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{seed}.png"
    if mode == "inpaint_bg":
        _paste_original(Path(frame_file), gen_bytes, Path(mask_file), out_file)
    else:
        out_file.write_bytes(gen_bytes)
    result = {"file": str(out_file), "meta_file": str(out_file.with_suffix(".json")), "seed": seed,
              "workflow": wf_path.stem, "prompt": str(prompt), "mode": mode, "model": _model_of(wf),
              "ai_generated": True, "warnings": warnings}
    meta = dict(result, negative=str(negative or ""), denoise=denoise, size=[w, h],
                source_frame=str(frame_file) if frame_file else None,
                mask_file=str(mask_file) if mask_file else None,
                face_preserved_by_paste=(mode == "inpaint_bg"),
                created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duration_sec=round(time.monotonic() - started, 2))
    Path(result["meta_file"]).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
