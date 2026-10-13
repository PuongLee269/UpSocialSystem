# Workflow ComfyUI cho thumbnail AI

Các file `*.json` ở đây là workflow **API format**. App thay placeholder trước khi gửi tới ComfyUI:
`{{prompt}} {{negative}} {{image}} {{mask}} {{seed}} {{denoise}} {{width}} {{height}}`.
Ba file mẫu (SDXL) chạy được nếu bạn có `sd_xl_base_1.0.safetensors`; đổi `ckpt_name` nếu dùng model khác.

## Cài ComfyUI (tự làm, app không cài hộ)
1. Tải ComfyUI portable (Windows, NVIDIA) từ github.com/comfyanonymous/ComfyUI, giải nén, chạy `run_nvidia_gpu.bat`.
2. Mặc định chạy ở `http://127.0.0.1:8188`. Điền vào `config.json`: `"comfyui_url": "http://127.0.0.1:8188"`, `"comfyui_workflow": "img2img_sdxl"`.
3. Với GGUF cần thêm custom node `ComfyUI-GGUF` (cài qua ComfyUI-Manager).

## Model gợi ý cho RTX 3080 Ti 12GB
- **SDXL** (.safetensors, ~6.5GB): nhanh, nhẹ, đặt vào `models/checkpoints`.
- **FLUX.1-schnell GGUF** (Q5/Q6): nhanh (4 bước), chất lượng tốt; đặt vào `models/unet`.
- **FLUX.1-dev GGUF** (Q4/Q5): đẹp hơn nhưng chậm hơn; cần thêm T5, CLIP-L và VAE riêng.
- **Qwen-Image-Edit GGUF** (Q4/Q5): sửa ảnh theo chỉ dẫn, hợp img2img/đổi nền.
Kiểm tra giấy phép từng model trước khi dùng thương mại. App không tự tải model.

## Xuất workflow
Trong ComfyUI: Settings, bật **Dev mode options**, rồi dùng **Save (API Format)** / Export (API).
Mở file JSON, thay giá trị cần điều khiển bằng placeholder (ví dụ `"text": "{{prompt}}"`, `"seed": "{{seed}}"`,
`"image": "{{image}}"` ở node LoadImage). Đặt file vào thư mục này; tên file (bỏ `.json`) là tên workflow.

## Chế độ
- `txt2img`: không cần ảnh gốc; dùng `{{width}} {{height}}`.
- `img2img`: dùng `{{image}}` và `{{denoise}}` (0.3-0.6 giữ bố cục).
- `inpaint_bg`: thêm `{{mask}}` (LoadImageMask, channel red). Mask đưa vào app: **trắng = người/mặt giữ nguyên**.
  App tự đảo mask cho workflow (trắng = vùng được sinh) và sau khi sinh **ghép lại pixel gốc** vùng người,
  nên khuôn mặt không đổi dù model vẽ lại.

Mọi ảnh tạo ra có sidecar `.json` (prompt, seed, workflow, model) và nhãn `ai_generated: true`.
