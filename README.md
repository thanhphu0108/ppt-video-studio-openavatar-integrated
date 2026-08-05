# PPT Video Studio

Ứng dụng Streamlit chuyển PowerPoint `.pptx` thành video thuyết minh tiếng Việt, tích hợp **OpenAvatar Runtime** để dùng GPU local của máy người dùng trong khi frontend vẫn chạy trên Streamlit Community Cloud.

## Tính năng hoàn chỉnh

- Đọc và làm sạch nội dung PowerPoint.
- Render nguyên hình slide bằng LibreOffice + Poppler.
- Giữ nền, ảnh, biểu đồ, SmartArt, bảng và bố cục ở dạng tĩnh.
- Phân loại slide và sinh lời thuyết minh tiếng Việt.
- Storyboard chỉnh sửa từng slide.
- Chọn/bỏ slide và chỉnh khoảng nghỉ.
- Từ điển phát âm JSON do người dùng quản lý.
- Import/export từ điển.
- Kiểm duyệt từ ngữ không phù hợp khi thêm, import và render.
- Chọn slide PowerPoint, slide hệ thống hoặc ảnh tải lên làm intro/outro.
- TTS riêng từng slide bằng Edge TTS.
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

## Giới hạn

- Animation và transition PowerPoint được làm phẳng thành ảnh tĩnh.
- Video/âm thanh nhúng trong PPTX chưa được phát lại.
- Streamlit Community Cloud có giới hạn CPU, RAM và thời gian xử lý.
- Checkpoint/model bên thứ ba không được đóng gói trong repository.
- Cần xem giấy phép riêng của Wav2Lip và model pretrained trước khi dùng thương mại.
