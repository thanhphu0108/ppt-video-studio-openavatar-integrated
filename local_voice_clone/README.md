# Local Voice Clone Service

Voice cloning offline cho PPT Video Studio. Server chỉ lắng nghe tại
`http://127.0.0.1:8009`; không tự mở LAN/Internet và không gửi reference audio,
transcript hay narration lên cloud.

```text
storyboard.xlsx -> Voice Clone :8009 -> WAV -> OpenAvatar/Wav2Lip :8008 -> video
```

Engine production là F5-TTS. Engine `dummy` chỉ tạo âm kiểm thử cho API/cache/
storyboard, không đọc và không clone giọng nói.

## Quyền dùng giọng

Chỉ dùng mẫu giọng nếu bạn sở hữu hoặc có đồng ý rõ ràng của chủ sở hữu. Khi
upload `reference_audio` qua UI/API, service cần:

1. Xác nhận quyền sử dụng giọng (`voice_use_consent=true`).
2. Mật khẩu bảo vệ upload qua header `X-Voice-Upload-Password`.

Mật khẩu mặc định chỉ có dạng PBKDF2-derived key trong mã. Có thể thay qua
`VOICE_UPLOAD_PASSWORD`; không đưa mật khẩu vào Git, log, Excel hay API key.

Checkpoint F5 được đặt trong `models/` mặc định (`MODEL_CACHE_DIR`); không
được đưa checkpoint vào Git.

## Cài đặt Windows

Yêu cầu Python 3.10+, FFmpeg trong `PATH`, và dung lượng cho checkpoint local.

```powershell
ffmpeg -version
cd C:\Phu\ppt-video-studio-openavatar-integrated\local_voice_clone
.\install.bat
```

Sau đó, trong `.venv`, cài PyTorch CPU/CUDA phù hợp máy theo hướng dẫn chính
thức của PyTorch. Không hard-code một CUDA wheel không phù hợp GPU. Cài F5-TTS:

```powershell
.\.venv\Scripts\activate
pip install -r requirements-f5.txt
```

Lấy checkpoint theo [F5-TTS chính thức](https://github.com/SWivid/F5-TTS) khi
bạn chủ động cho phép Internet. Mặc định `ALLOW_MODEL_DOWNLOAD=false`, nên
service không tự tải checkpoint khi chạy. Sau khi cache model local, chạy:

```powershell
.\start.bat
```

Mở:

```text
http://127.0.0.1:8009/       UI local
http://127.0.0.1:8009/docs   Swagger
http://127.0.0.1:8009/health health check
```

Dừng đúng process bằng `stop.bat`. Script chỉ dừng PID do service ghi và kiểm
tra command line trước khi dừng; không kill toàn bộ Python.

## Voice profile

Tạo:

```text
voices/default/reference.wav
voices/default/transcript.txt
```

Khuyến nghị WAV mono, rõ tiếng, một người nói, 3–12 giây. Transcript phải đúng
lời trong mẫu. API/UI nhận WAV, MP3, M4A, AAC, OGG, FLAC; service chuyển về
mono WAV 24 kHz, trim im lặng dài và normalize bằng FFmpeg/soundfile.

## PPT Video Studio

Chạy app PPT trên cùng máy rồi chọn `Nhân bản giọng từ mẫu (API riêng)`:

```text
Voice-clone API endpoint: http://127.0.0.1:8009/v1/voice-clone/synthesize
Model:                    f5-tts
API key:                  để trống
Nội dung mẫu giọng:       transcript đúng với reference audio
```

Form đã đặt endpoint/model này làm mặc định. Mật khẩu giọng upload chỉ được
gửi tới hostname loopback, không bị gửi sang endpoint cloud. Nếu app Streamlit
được host trên cloud, chạy app PPT local để Python gọi `127.0.0.1` được.

## API

```powershell
Invoke-RestMethod http://127.0.0.1:8009/health
Invoke-RestMethod http://127.0.0.1:8009/v1/models
Invoke-RestMethod http://127.0.0.1:8009/v1/voices
```

Nếu `LOCAL_API_KEY` được đặt, thêm `Authorization: Bearer <key>`. Khi để trống,
auth tắt mặc định.

### JSON với profile đã đăng ký

```powershell
$body = @{
  model = "f5-tts"
  voice_id = "default"
  text = "Kính thưa quý anh chị, hôm nay chúng ta cùng trao đổi về trải nghiệm người bệnh."
  reference_text = "Xin chào quý anh chị, đây là mẫu giọng dùng để tổng hợp tiếng nói."
  language = "vi"
  speed = 1.0
  output_format = "wav"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8009/v1/voice-clone/synthesize" -Method Post -ContentType "application/json" -Body $body
```

Response có `audio_path`, `audio_url`, `duration_seconds`, `warnings` và
`cache_hit`. Thêm `return_audio=true` để nhận file trực tiếp. WAV mono PCM 16
bit là default/khuyến nghị cho Wav2Lip; MP3 là 192 kbps.

### Upload mẫu giọng

`/v1/voice-clone/synthesize` và `/v1/voice-clone/synthesize-upload` đều nhận
multipart fields: `model`, `text`, `reference_audio`, `reference_text` hoặc
`reference_transcript`, `output_format`, `voice_use_consent`.

```powershell
$headers = @{ "X-Voice-Upload-Password" = "<mật khẩu upload>" }
$form = @{
  model = "f5-tts"; text = "Kính thưa quý anh chị."; reference_text = "Xin chào quý anh chị."
  output_format = "wav"; voice_use_consent = "true"
  reference_audio = Get-Item ".\voices\default\reference.wav"
}
Invoke-RestMethod -Uri "http://127.0.0.1:8009/v1/voice-clone/synthesize-upload" -Method Post -Headers $headers -Form $form
```

Thiếu transcript sẽ có warning `REFERENCE_TRANSCRIPT_MISSING`; service không
tự bịa transcript.

## Storyboard Excel

Sheet phải tên `Storyboard`, với cột:

```text
Slide | Tiêu đề | Lời thuyết minh | Xuất | Nghỉ sau (giây)
```

Chỉ dòng `Xuất=TRUE` được sinh. CLI tạo `slide_001.wav`... và
`generated_audio/manifest.json`:

```powershell
.\.venv\Scripts\python.exe synthesize_storyboard.py C:\duong-dan\storyboard.xlsx --voice-id default --format wav
```

CLI hiển thị `[1/14] Slide 1 ... done 72.4s`; mặc định dừng khi lỗi, dùng
`--continue-on-error` để đi tiếp. Test flow mà chưa cài F5 với `--model dummy`
và reference WAV hợp lệ (đây không phải giọng nói thật).

Nếu dùng `--reference-audio` thay cho profile đã đăng ký, thêm
`--confirm-voice-use` để xác nhận bạn có quyền dùng mẫu giọng.

## Wav2Lip/OpenAvatar hook

Giữ hai server độc lập:

```text
Voice Clone: http://127.0.0.1:8009
OpenAvatar:  http://127.0.0.1:8008
```

Sau khi OpenAvatar Runtime chạy, pipeline local tùy chọn là:

```powershell
.\.venv\Scripts\python.exe pipeline_storyboard_wav2lip.py storyboard.xlsx face.png --voice-id default
```

Adapter gọi `WAV2LIP_ENDPOINT` trong `.env`, tạo narration WAV, gửi
`/avatar/generate`, chờ job và tải video. Cổng 8008 không hard-code trong
business logic.

## Vận hành

Model load một lần trong process. Cache hash gồm reference, transcript, text,
model, speed và format. Text dài được chunk theo `MAX_CHARS_PER_CHUNK=350`;
có pause giữa câu/paragraph. GPU OOM xóa CUDA cache rồi thử lại một lần với
chunk nhỏ hơn. CPU fallback không làm service chết nhưng sẽ chậm.

Log local là `logs/voice_clone.log`, không có audio binary/API secret/full text
mặc định. Mã lỗi gồm `REFERENCE_AUDIO_INVALID`, `MODEL_NOT_LOADED`,
`UNSUPPORTED_AUDIO_FORMAT`, `FFMPEG_NOT_FOUND`, `GPU_OUT_OF_MEMORY` và lỗi
quyền/mật khẩu upload. Không bật telemetry hoặc cloud TTS/transcription.
