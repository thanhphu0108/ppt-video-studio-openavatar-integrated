# Voice-clone API contract

PPT Video Studio hỗ trợ dịch vụ nhân bản giọng do đơn vị triển khai tự vận hành. Cách này tránh phải tải model TTS/clone giọng rất lớn vào Streamlit Cloud và cho phép giữ audio trong hạ tầng do người dùng kiểm soát.

## Request

Ứng dụng gửi `POST` đến URL nhập trong tab **4. Xuất video** dưới dạng `multipart/form-data`.

| Field | Kiểu | Bắt buộc | Nội dung |
| --- | --- | --- | --- |
| `reference_audio` | file | Có | File mẫu giọng người dùng tải lên. |
| `text` | string | Có | Lời cần đọc của một slide. |
| `model` | string | Có | Giá trị người dùng cấu hình, mặc định `default`. |
| `reference_transcript` | string | Không | Transcript của mẫu giọng nếu model cần. |
| `output_format` | string | Có | `mp3` (hoặc phần mở rộng audio đích). |

Nếu người dùng nhập API key, ứng dụng thêm header:

```http
Authorization: Bearer <API_KEY>
```

## Response

Endpoint có thể trả một trong hai dạng:

1. Dữ liệu audio thô với `Content-Type: audio/mpeg`, `audio/wav`, v.v.
2. JSON gồm một trong các field sau:

```json
{ "audio_base64": "..." }
```

hoặc

```json
{ "audio_url": "https://.../generated-audio.mp3" }
```

Với lỗi, trả HTTP 4xx/5xx và JSON có `error` hoặc `message` để app hiển thị nguyên nhân.

## Bảo mật và quyền sử dụng giọng

- Mẫu giọng chỉ được giữ trong phiên xuất và không được ghi vào project JSON.
- Endpoint/API key không được đưa vào file project tải xuống.
- UI yêu cầu người dùng xác nhận họ sở hữu giọng hoặc có sự đồng ý rõ ràng của người sở hữu trước khi tạo audio.
- Dùng HTTPS trong môi trường production và cấu hình chính sách lưu/xóa audio tại dịch vụ clone giọng của bạn.
