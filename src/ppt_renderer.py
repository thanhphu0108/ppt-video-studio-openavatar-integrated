from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


class PowerPointRenderError(RuntimeError):
    pass


def _find_command(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def render_pptx_slides(payload: bytes, filename: str = "presentation.pptx", *, dpi: int = 144) -> tuple[list[Image.Image], str]:
    """Render complete PPTX slides through LibreOffice -> PDF -> PNG.

    This preserves backgrounds, images, charts, SmartArt and layout much better than
    rebuilding slides from extracted text. PowerPoint animations and embedded video
    are flattened to their static slide appearance.
    """
    libreoffice = _find_command("libreoffice", "soffice")
    pdftoppm = _find_command("pdftoppm")
    if not libreoffice:
        raise PowerPointRenderError("Không tìm thấy LibreOffice trên máy chủ.")
    if not pdftoppm:
        raise PowerPointRenderError("Không tìm thấy pdftoppm (gói poppler-utils).")

    with tempfile.TemporaryDirectory(prefix="pptx_render_") as tmp:
        work = Path(tmp)
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".pptx"):
            safe_name += ".pptx"
        pptx_path = work / safe_name
        pptx_path.write_bytes(payload)

        profile_dir = work / "lo_profile"
        profile_dir.mkdir()
        command = [
            libreoffice,
            f"-env:UserInstallation=file://{profile_dir.as_posix()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(work),
            str(pptx_path),
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        pdf_path = work / f"{pptx_path.stem}.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            detail = (result.stderr or result.stdout or "LibreOffice không tạo được PDF.").strip()
            raise PowerPointRenderError(detail[-1200:])

        prefix = work / "slide"
        scale = max(72, int(dpi))
        result = subprocess.run(
            [pdftoppm, "-png", "-r", str(scale), str(pdf_path), str(prefix)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
        )
        paths = sorted(work.glob("slide-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        if result.returncode != 0 or not paths:
            detail = (result.stderr or "Không chuyển được PDF sang ảnh.").strip()
            raise PowerPointRenderError(detail[-1200:])

        images: list[Image.Image] = []
        for path in paths:
            with Image.open(path) as image:
                images.append(image.convert("RGB").copy())
        return images, "LibreOffice + Poppler"
