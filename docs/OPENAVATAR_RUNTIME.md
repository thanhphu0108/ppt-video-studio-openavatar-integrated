# OpenAvatar Runtime Integration

The browser bridge uses the OpenAvatar Runtime v3 contract.

## Health

`GET /health`

## Generate

`POST /avatar/generate`

Multipart:

- `image`
- `audio`
- `engine`

## Poll

`GET /jobs/{job_id}`

Terminal states:

- `completed`
- `failed`
- `cancelled`

## Download

`GET /jobs/{job_id}/download`

Returns MP4 bytes.
