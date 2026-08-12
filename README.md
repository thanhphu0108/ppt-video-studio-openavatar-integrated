# PPT Video Studio

Ứng dụng Streamlit chuyển PowerPoint `.pptx` thành video thuyết minh tiếng Việt, tích hợp **OpenAvatar Runtime** để dùng GPU local của máy người dùng trong khi frontend vẫn chạy trên Streamlit Community Cloud.

## Tính năng hoàn chỉnh

- Đọc và làm sạch nội dung PowerPoint.
- Render nguyên hình slide bằng LibreOffice + Poppler.
- Giữ nền, ảnh, biểu đồ, SmartArt, bảng và bố cục ở dạng tĩnh.
- Phân loại slide và sinh lời thuyết minh tiếng Việt.
- Storyboard chỉnh sửa từng slide, tải mẫu CSV/Excel và nhập CSV/Excel/JSON project.
- Chọn/bỏ slide và chỉnh khoảng nghỉ.
- Từ điển phát âm JSON do người dùng quản lý.
- Import/export từ điển.
- Kiểm duyệt từ ngữ không phù hợp khi thêm, import và render.
- Chọn slide PowerPoint, slide hệ thống hoặc ảnh tải lên làm intro/outro, mỗi loại đều có lời thuyết minh riêng.
- Ba nguồn giọng: Edge TTS, bản thu thật theo từng slide và nhân bản giọng qua API riêng có xác nhận quyền sử dụng giọng.
- Các luồng tải audio giọng được bảo vệ bằng mật khẩu theo phiên sử dụng.
- Phụ đề đốt vào video và file SRT.
- Fade, zoom nhẹ và hiệu ứng người dẫn.
- Ảnh người dẫn tĩnh hoặc hiệu ứng nói nhẹ.
- AI nhép môi qua GPU API từ xa.
- AI nhép môi bằng OpenAvatar Runtime trên GPU local.
- Xuất MP4, SRT, TXT và project JSON.

## Kiến trúc OpenAvatar Runtime

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

Streamlit Cloud không gọi trực tiếp `localhost`. Custom Streamlit Component trong repository này thực hiện request từ trình duyệt:

1. `GET /health`
2. `POST /avatar/generate`
3. Poll `GET /jobs/{job_id}`
4. `GET /jobs/{job_id}/download`
5. Chuyển MP4 về phiên Streamlit để ghép vào slide.

## Chạy local

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

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

Chọn **Nhân bản giọng từ mẫu (API riêng)**, tải mẫu giọng rõ tiếng 15–60 giây, nhập endpoint của dịch vụ clone giọng và xác nhận quyền sử dụng giọng. Ứng dụng không đóng gói sẵn model clone giọng nặng; endpoint có thể là dịch vụ nội bộ hoặc GPU service do bạn kiểm soát. Hợp đồng endpoint ở [docs/VOICE_CLONE_API.md](docs/VOICE_CLONE_API.md).

## Giới hạn

- Animation và transition PowerPoint được làm phẳng thành ảnh tĩnh.
- Video/âm thanh nhúng trong PPTX chưa được phát lại.
- Streamlit Community Cloud có giới hạn CPU, RAM và thời gian xử lý.
- Checkpoint/model bên thứ ba không được đóng gói trong repository.
- Cần xem giấy phép riêng của Wav2Lip và model pretrained trước khi dùng thương mại.
- Chỉ dùng mẫu giọng khi bạn là chủ sở hữu hoặc có sự đồng ý rõ ràng của người sở hữu giọng.
