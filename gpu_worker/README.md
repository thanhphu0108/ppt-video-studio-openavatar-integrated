# GPU Talking-Head Worker Contract

PPT Video Studio v1.3 calls a separate GPU service.

## Endpoints

### `GET /health`

Returns HTTP 200, for example:

```json
{"status":"ok","engines":["wav2lip","sadtalker","musetalk","liveportrait"]}
```

### `POST /generate`

Multipart form fields:

- `image`: portrait PNG/JPG
- `audio`: MP3/WAV narration
- `engine`: `wav2lip`, `sadtalker`, `musetalk`, or `liveportrait`
- `preview_seconds`: optional

The endpoint may return:

1. MP4 bytes directly; or
2. `{"video_url":"https://.../result.mp4"}`; or
3. `{"status_url":"https://.../jobs/123"}` for polling.

Use authorization header `Bearer <AVATAR_API_KEY>` when configured.

## Recommended deployment

- RunPod Serverless or Pod
- Modal GPU function
- Hugging Face GPU Space
- A local/VPS NVIDIA GPU with FastAPI

The Streamlit app does not bundle model checkpoints because they are large and each project's license/usage terms must be reviewed separately.
