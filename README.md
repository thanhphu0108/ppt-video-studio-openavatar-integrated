# PPT Video Studio

Ứng dụng Streamlit chuyển PowerPoint `.pptx` thành video thuyết minh tiếng Việt, tích hợp **OpenAvatar Runtime** để dùng GPU local của máy người dùng trong khi frontend vẫn chạy trên Streamlit Community Cloud.

## Tính năng hoàn chỉnh

- Đọc và làm sạch nội dung PowerPoint.
- Render nguyên hình slide bằng Microsoft PowerPoint COM trên Windows; fallback sang LibreOffice + Poppler/PyMuPDF.
- Giữ nền, ảnh, biểu đồ, SmartArt, bảng và bố cục ở dạng tĩnh.
- Phân loại slide và sinh lời thuyết minh tiếng Việt.
- Storyboard chỉnh sửa từng slide, tải mẫu CSV/Excel và nhập CSV/Excel/JSON project.
- Chọn/bỏ slide và chỉnh khoảng nghỉ.
- Từ điển phát âm JSON do người dùng quản lý.
- Import/export từ điển.
- Kiểm duyệt từ ngữ không phù hợp khi thêm, import và render.
- Chọn slide PowerPoint, slide hệ thống hoặc ảnh tải lên làm intro/outro, mỗi loại đều có lời thuyết minh riêng.
- Bốn nguồn giọng: Edge TTS, VieNeu-TTS local, bản thu thật theo từng slide và nhân bản giọng tiếng Việt bằng VieNeu v3 Turbo qua API riêng có xác nhận quyền sử dụng giọng.
- Các luồng tải audio giọng được bảo vệ bằng mật khẩu theo phiên sử dụng.
- Phụ đề đốt vào video và file SRT.
- Phụ đề đốt chạy kiểu karaoke theo cụm ngắn, cố định khung và tự ẩn sau lời nói; cỡ chữ cấu hình từ 10 px.
- Tùy chỉnh màu nền, màu chữ, độ rộng khung theo phần trăm và căn giữa/góc cho phụ đề.
- Có chế độ giữ nguyên khung slide PPT, tắt zoom/crop/fade để mọi nguồn audio dùng cùng hình gốc.
- Fade, zoom nhẹ và hiệu ứng người dẫn.
- Ảnh người dẫn tĩnh hoặc hiệu ứng nói nhẹ.
- AI nhép môi qua GPU API từ xa.
- AI nhép môi bằng OpenAvatar Runtime trên GPU local.
- Xuất MP4, SRT, TXT và project JSON.

## Kiến trúc giọng local và OpenAvatar Runtime

```text
PPT Video Studio trên Streamlit Cloud
              │
              ▼
Browser bridge trong trình duyệt người dùng
              │
              ▼
OpenAvatar Runtime
http://127.0.0.1:8008
              │
              ▼
GPU NVIDIA local + Wav2Lip
```

Voice clone là service riêng, không phải OpenAvatar Runtime:

```text
Storyboard / lời thuyết minh
              │
              ▼
Local Voice Service (VieNeu v3 Turbo / F5-TTS / Vira-TTS)
http://127.0.0.1:8009
              │ WAV mono
              ▼
OpenAvatar Runtime / Wav2Lip
http://127.0.0.1:8008
              │
              ▼
Talking-head video
```

`8008` chỉ nhận audio đã có để nhép môi; không clone hoặc render giọng. Hướng
dẫn cài Local Voice Clone Service ở [local_voice_clone/README.md](local_voice_clone/README.md).

VieNeu cũng đi qua service `8009`: Browser Bridge gửi text/voice ID từ trình
duyệt tới service local, service nạp model và GPU trên máy người dùng rồi trả
WAV về Streamlit. Vì vậy Streamlit Cloud không cần cài hoặc tải model VieNeu.

Để nhân bản giọng tiếng Việt nhanh, chạy
`local_voice_clone/start_vieneu_clone_8009.bat`, sau đó chọn nguồn **Nhân bản
giọng từ mẫu (API riêng)** và model `vieneu-clone`. Trong ô **Chế độ vùng giọng**,
chọn **Miền Nam/Bắc/Trung**. VieNeu giữ speaker embedding của file mẫu và dùng
reference codes của prompt vùng đã chọn để tránh rơi về preset Bắc mặc định. Mẫu
3–8 giây, nạp model một lần và tái sử dụng cho các slide. `f5-tts` hiện là
checkpoint Trung/Anh; `vira-tts` vẫn giữ làm phương án tiếng Việt dự phòng
nhưng thường chậm hơn.

Streamlit Cloud không gọi trực tiếp `localhost`. Custom Streamlit Component trong repository này thực hiện request từ trình duyệt:

1. `GET /health`
2. `POST /avatar/generate`
3. Poll `GET /jobs/{job_id}`
4. `GET /jobs/{job_id}/download`
5. `GET /v1/vieneu/voices` và `POST /v1/voice-clone/synthesize` hoặc
   `POST /v1/storyboard/synthesize` cho VieNeu local.
6. Chuyển MP4/WAV về phiên Streamlit để ghép vào slide.

## Chạy local

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Trên Windows, nếu dùng PowerPoint để render, `requirements.txt` sẽ cài thêm
`pywin32`; app tự khởi tạo COM trong thread của Streamlit. Nếu máy không có
PowerPoint, cài LibreOffice để dùng renderer dự phòng.

### VieNeu-TTS local

Trong tab **4. Xuất video**, chọn **VieNeu-TTS local**. App dùng Browser Bridge
trong `local_gpu_component/index.html` để lấy danh sách preset và gửi từng đoạn
text tới `http://127.0.0.1:8009`; model/audio không chạy trên Streamlit Cloud.
VieNeu-TTS hiện yêu cầu Python 3.10+ và được cài trong môi trường của
`local_voice_clone`:

```bat
cd local_voice_clone
python -m pip install -r requirements.txt
start_vieneu_clone_8009.bat
```

Để không giữ terminal/IDE, có thể chạy `start_vieneu_clone_8009_background.bat`;
log service nằm trong `local_voice_clone/logs/`.

Nếu môi trường local đã cài service trước đó, cập nhật riêng:

```bat
python -m pip install -U vieneu
```

Mặc định script clone dùng `VIENEU_BACKEND=pytorch` để tận dụng GPU. Có thể đặt
`VIENEU_BACKEND=onnx` để buộc backend ONNX/CPU cho giọng preset; backend ONNX
không phải lựa chọn clone GPU chính. Để chạy VieNeu
trực tiếp trong Python của Streamlit local (không khuyến nghị khi deploy Cloud),
đặt `VIENEU_DIRECT_PYTHON=true`.

## Deploy Streamlit Community Cloud

1. Đẩy repository lên GitHub.
2. Tạo app mới trên Streamlit Community Cloud.
3. Chọn `app.py`.
4. Giữ nguyên `requirements.txt` và `packages.txt`.

`packages.txt` cài:

- FFmpeg
- LibreOffice
- Poppler
- Font DejaVu, Liberation và Noto

## Chuẩn bị OpenAvatar Runtime

OpenAvatar Runtime phải chạy trên máy người dùng:

```text
http://127.0.0.1:8008/health
http://127.0.0.1:8008/docs
```

Trong app chọn:

```text
Người dẫn → AI nhép môi bằng OpenAvatar Runtime
```

Sau đó:

1. Tải ảnh người dẫn.
2. Chọn Wav2Lip.
3. Kiểm tra kết nối Runtime.
4. Chuẩn bị audio cho từng slide.
5. Tạo clip avatar.
6. Bấm **Tạo video**.

## Storyboard và giọng đọc

### Nhập Storyboard

Trong tab **2. Storyboard**, bấm tải mẫu CSV hoặc Excel. File mẫu đã có đúng số slide của PowerPoint hiện tại; có thể sửa các cột `Tiêu đề`, `Lời thuyết minh`, `Xuất` và `Nghỉ sau (giây)` rồi nhập lại. App cũng nhận project JSON đã xuất trước đó.

### Dùng bản thu giọng thật

Trong tab **4. Xuất video** chọn **Bản thu thật theo từng slide**. Tải các file với quy ước:

```text
slide_001.mp3
slide_002.wav
intro.mp3    # nếu có mở đầu
outro.mp3    # nếu có kết thúc
```

App dùng nguyên audio đã tải lên, ghép đúng theo từng slide và dùng chính audio này cho avatar nhép môi. Bản thu được giữ trong phiên xuất, không được đưa vào project JSON.

Trước khi hiện ô tải audio, ứng dụng yêu cầu mật khẩu. Sau khi mở khóa, có thể bấm **Khóa lại** để xóa audio, mẫu giọng và thông tin API khỏi phiên đang dùng. Khi deploy, có thể thay mật khẩu mặc định mà không sửa mã bằng biến môi trường hoặc Streamlit secret `VOICE_UPLOAD_PASSWORD`.

### Nhân bản giọng từ mẫu

Chọn **Nhân bản giọng từ mẫu (API riêng)**, mở khóa tính năng tải giọng, tải
mẫu giọng rõ tiếng và xác nhận quyền sử dụng giọng. Local service đi kèm chạy
hoàn toàn trên máy, hỗ trợ `.wav`, `.mp3`, `.m4a`, `.aac`, `.ogg`, `.flac` và
chuẩn hóa mẫu thành mono WAV trước khi F5-TTS chạy.

Giá trị mặc định của form:

```text
Voice-clone API endpoint: http://127.0.0.1:8009/v1/voice-clone/synthesize
Model:                    f5-tts
API key:                  để trống
Nội dung mẫu giọng:       transcript đúng với reference audio
```

WAV được ưu tiên làm source cho OpenAvatar/Wav2Lip; app tự yêu cầu WAV khi
endpoint là local `127.0.0.1`. Endpoint có thể thay bằng service nội bộ khác
do bạn kiểm soát. Xem [hợp đồng API](docs/VOICE_CLONE_API.md) và [cài service
local](local_voice_clone/README.md).

## Giới hạn

- Animation và transition PowerPoint được làm phẳng thành ảnh tĩnh.
- Video/âm thanh nhúng trong PPTX chưa được phát lại.
- Streamlit Community Cloud có giới hạn CPU, RAM và thời gian xử lý.
- Checkpoint/model bên thứ ba không được đóng gói trong repository.
- Cần xem giấy phép riêng của Wav2Lip và model pretrained trước khi dùng thương mại.
- Chỉ dùng mẫu giọng khi bạn là chủ sở hữu hoặc có sự đồng ý rõ ràng của người sở hữu giọng.
