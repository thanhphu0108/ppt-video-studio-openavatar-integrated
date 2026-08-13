from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config.settings import get_settings
from services.errors import VoiceCloneServiceError
from services.lipsync_client import LipSyncClient
from synthesize_storyboard import build_parser as storyboard_parser
from synthesize_storyboard import read_storyboard
from services.synthesis_service import SynthesisService


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Pipeline local: Storyboard -> Voice Clone WAV -> OpenAvatar/Wav2Lip.")
    command.add_argument("storyboard", help="storyboard.xlsx, sheet Storyboard")
    command.add_argument("face_input", help="Ảnh hoặc video khuôn mặt đưa vào OpenAvatar")
    command.add_argument("--voice-id", default=None)
    command.add_argument("--model", default=None)
    command.add_argument("--reference-audio", default=None)
    command.add_argument("--reference-text", default=None)
    command.add_argument("--confirm-voice-use", action="store_true")
    command.add_argument("--speed", type=float, default=1.0)
    command.add_argument("--audio-dir", default=None)
    command.add_argument("--video-dir", default="generated_videos")
    command.add_argument("--continue-on-error", action="store_true")
    return command


def run(args: argparse.Namespace) -> int:
    try:
        slides = read_storyboard(args.storyboard)
    except (OSError, ValueError) as exc:
        print(f"Lỗi storyboard: {exc}", file=sys.stderr)
        return 2
    if args.reference_audio and not args.confirm_voice_use:
        print("Cần thêm --confirm-voice-use khi dùng --reference-audio.", file=sys.stderr)
        return 2
    settings = get_settings()
    service = SynthesisService(settings)
    service.warm_up()
    lipsync = LipSyncClient(settings.wav2lip_endpoint)
    try:
        lipsync.health()
    except VoiceCloneServiceError as exc:
        print(f"Lỗi OpenAvatar/Wav2Lip: {exc.message}", file=sys.stderr)
        return 3
    audio_dir = Path(args.audio_dir).resolve() if args.audio_dir else settings.output_dir.resolve()
    video_dir = Path(args.video_dir).resolve()
    audio_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    had_error = False
    for index, slide in enumerate(slides, start=1):
        number = int(slide["slide"])
        try:
            audio = service.synthesize(
                model=args.model,
                voice_id=args.voice_id,
                reference_audio=args.reference_audio,
                reference_text=args.reference_text,
                text=slide["text"],
                speed=args.speed,
                output_format="wav",
                output_dir=audio_dir,
                output_name=f"slide_{number:03d}",
            )
            video_path = video_dir / f"slide_{number:03d}.mp4"
            lipsync.synthesize_video(args.face_input, audio.audio_path, video_path)
            records.append({"slide": number, "audio": str(audio.audio_path), "video": str(video_path), "status": "SUCCESS"})
            print(f"[{index}/{len(slides)}] Slide {number} ... video done")
        except VoiceCloneServiceError as exc:
            had_error = True
            records.append({"slide": number, "status": "FAILED", "error_code": exc.code, "message": exc.message})
            print(f"[{index}/{len(slides)}] Slide {number} ... failed {exc.code}: {exc.message}", file=sys.stderr)
            if not args.continue_on_error:
                break
    (video_dir / "pipeline_manifest.json").write_text(json.dumps({"success": not had_error, "wav2lip_endpoint": settings.wav2lip_endpoint, "slides": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
