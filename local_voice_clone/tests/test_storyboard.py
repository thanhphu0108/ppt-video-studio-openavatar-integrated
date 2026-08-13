from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import Workbook

from synthesize_storyboard import read_storyboard, run


def test_storyboard_batch_generates_multiple_audio_and_manifest(local_env, reference_wav) -> None:
    workbook_path = local_env / "storyboard.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Storyboard"
    sheet.append(["Slide", "Tiêu đề", "Lời thuyết minh", "Xuất", "Nghỉ sau (giây)"])
    sheet.append([1, "Mở đầu", "Kính thưa quý anh chị.", True, 0.35])
    sheet.append([2, "Nội dung", "Chúng ta cùng trao đổi về trải nghiệm người bệnh.", True, 0.5])
    sheet.append([3, "Không xuất", "Không sinh audio này.", False, 0])
    workbook.save(workbook_path)
    assert [item["slide"] for item in read_storyboard(workbook_path)] == [1, 2]

    output_dir = local_env / "batch_output"
    code = run(
        argparse.Namespace(
            storyboard=str(workbook_path),
            voice_id=None,
            model="dummy",
            reference_audio=str(reference_wav),
            reference_text="Xin chào quý anh chị.",
            confirm_voice_use=True,
            format="wav",
            speed=1.0,
            output_dir=str(output_dir),
            continue_on_error=False,
        )
    )
    assert code == 0
    assert (output_dir / "slide_001.wav").exists()
    assert (output_dir / "slide_002.wav").exists()
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["success"] is True
    assert [item["status"] for item in manifest["slides"]] == ["SUCCESS", "SUCCESS"]
