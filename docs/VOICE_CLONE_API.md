# Voice-clone API contract

PPT Video Studio hỗ trợ dịch vụ nhân bản giọng do đơn vị triển khai tự vận hành. Cách này tránh phải tải model TTS/clone giọng rất lớn vào Streamlit Cloud và cho phép giữ audio trong hạ tầng do người dùng kiểm soát.

## Request

Ứng dụng gửi `POST` đến URL nhập trong tab **4. Xuất video** dưới dạng `multipart/form-data`.

| Field | Kiểu | Bắt buộc | Nội dung |
| --- | --- | --- | --- |
| `reference_audio` | file | Có | File mẫu giọng người dùng tải lên. |
| `text` | string | Có | Lời cần đọc của một slide. |
| `model` | string | Có | Giá trị người dùng cấu hình, mặc định local là `f5-tts`. |
| `reference_transcript` | string | Không | Transcript của mẫu giọng nếu model cần. |
| `reference_text` | string | Không | Tên tương đương ưu tiên của transcript cho Local Voice Clone Service. |
| `output_format` | string | Có | `wav` hoặc `mp3`; local mặc định `wav`. |
| `voice_use_consent` | boolean/string | Có với mẫu upload local | `true` khi người dùng đã xác nhận quyền sử dụng giọng. |

Nếu người dùng nhập API key, ứng dụng thêm header:

```http
Authorization: Bearer <API_KEY>
```

Với Local Voice Clone Service ở `127.0.0.1:8009`, app còn gửi header
`X-Voice-Upload-Password` **chỉ tới loopback** để bảo vệ audio mẫu. Header này
không được gửi sang endpoint Internet. Service local chấp nhận cả JSON với
`voice_id` (profile đã đăng ký) và multipart upload.

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

Local service trả schema sau:

```json
{
  "success": true,
  "request_id": "uuid",
  "model": "f5-tts",
  "voice_id": "default",
  "audio_path": "generated_audio/uuid.wav",
  "audio_url": "http://127.0.0.1:8009/audio/uuid.wav",
  "duration_seconds": 12.84
}
```

Tài liệu cài, Swagger, batch Storyboard Excel và hook Wav2Lip nằm ở
[local_voice_clone/README.md](../local_voice_clone/README.md).

## Bảo mật và quyền sử dụng giọng

- Mẫu giọng chỉ được giữ trong phiên xuất và không được ghi vào project JSON.
- Endpoint/API key không được đưa vào file project tải xuống.
- UI yêu cầu người dùng xác nhận họ sở hữu giọng hoặc có sự đồng ý rõ ràng của người sở hữu trước khi tạo audio.
- Dùng HTTPS trong môi trường production và cấu hình chính sách lưu/xóa audio tại dịch vụ clone giọng của bạn.
