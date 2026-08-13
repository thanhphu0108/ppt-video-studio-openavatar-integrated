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


def _render_with_powerpoint_com(
    pptx_path: Path,
    work: Path,
) -> list[Image.Image]:
    try:
        import win32com.client
    except ImportError as exc:
        raise PowerPointRenderError(
            "Chưa cài pywin32 để dùng Microsoft PowerPoint COM."
        ) from exc

    export_dir = work / "ppt_export"
    export_dir.mkdir(parents=True, exist_ok=True)

    app = None
    presentation = None

    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")

        # Với một số bản Office, Visible=0 có thể không ổn định.
        # Không cần hiện UI, nhưng vẫn để PowerPoint chạy automation.
        try:
            app.Visible = 1
        except Exception:
            pass

        presentation = app.Presentations.Open(
            str(pptx_path.resolve()),
            WithWindow=False,
        )

        # PowerPoint ppSaveAsPNG = 18
        presentation.SaveAs(
            str(export_dir.resolve()),
            18,
        )

    except Exception as exc:
        raise PowerPointRenderError(
            f"Microsoft PowerPoint COM render thất bại: {exc}"
        ) from exc

    finally:
        try:
            if presentation is not None:
                presentation.Close()
        except Exception:
            pass

        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass

    # PowerPoint thường sinh Slide1.PNG, Slide2.PNG...
    paths = list(export_dir.glob("*.PNG"))
    if not paths:
        paths = list(export_dir.glob("*.png"))

    def slide_number(path: Path) -> int:
        digits = "".join(ch for ch in path.stem if ch.isdigit())
        return int(digits or 0)

    paths = sorted(paths, key=slide_number)

    if not paths:
        raise PowerPointRenderError(
            "PowerPoint COM không tạo được ảnh slide."
        )

    images: list[Image.Image] = []

    for path in paths:
        with Image.open(path) as image:
            images.append(
                image.convert("RGB").copy()
            )

    return images


def _render_with_libreoffice(
    pptx_path: Path,
    work: Path,
    *,
    dpi: int,
) -> list[Image.Image]:
    libreoffice = _find_command(
        "libreoffice",
        "soffice",
    )
    pdftoppm = _find_command("pdftoppm")

    if not libreoffice:
        raise PowerPointRenderError(
            "Không tìm thấy LibreOffice."
        )

    if not pdftoppm:
        raise PowerPointRenderError(
            "Không tìm thấy pdftoppm."
        )

    profile_dir = work / "lo_profile"
    profile_dir.mkdir(exist_ok=True)

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

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )

    pdf_path = work / f"{pptx_path.stem}.pdf"

    if result.returncode != 0 or not pdf_path.exists():
        detail = (
            result.stderr
            or result.stdout
            or "LibreOffice không tạo được PDF."
        ).strip()

        raise PowerPointRenderError(
            detail[-1200:]
        )

    prefix = work / "slide"
    scale = max(72, int(dpi))

    result = subprocess.run(
        [
            pdftoppm,
            "-png",
            "-r",
            str(scale),
            str(pdf_path),
            str(prefix),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
    )

    paths = sorted(
        work.glob("slide-*.png"),
        key=lambda p: int(
            p.stem.split("-")[-1]
        ),
    )

    if result.returncode != 0 or not paths:
        detail = (
            result.stderr
            or "Không chuyển được PDF sang ảnh."
        ).strip()

        raise PowerPointRenderError(
            detail[-1200:]
        )

    images: list[Image.Image] = []

    for path in paths:
        with Image.open(path) as image:
            images.append(
                image.convert("RGB").copy()
            )

    return images


def render_pptx_slides(
    payload: bytes,
    filename: str = "presentation.pptx",
    *,
    dpi: int = 144,
) -> tuple[list[Image.Image], str]:
    """Render nguyên hình slide PowerPoint.

    Windows:
        Ưu tiên Microsoft PowerPoint COM.

    Fallback:
        LibreOffice -> PDF -> Poppler PNG.

    Animation/video nhúng vẫn được flatten thành trạng thái tĩnh.
    """

    with tempfile.TemporaryDirectory(
        prefix="pptx_render_"
    ) as tmp:
        work = Path(tmp)

        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".pptx"):
            safe_name += ".pptx"

        pptx_path = work / safe_name
        pptx_path.write_bytes(payload)

        errors: list[str] = []

        # --------------------------------------------------
        # 1. Ưu tiên Microsoft PowerPoint trên Windows
        # --------------------------------------------------
        try:
            images = _render_with_powerpoint_com(
                pptx_path,
                work,
            )

            if images:
                return (
                    images,
                    "Microsoft PowerPoint COM",
                )

        except Exception as exc:
            errors.append(
                f"PowerPoint COM: {exc}"
            )

        # --------------------------------------------------
        # 2. Fallback LibreOffice
        # --------------------------------------------------
        try:
            images = _render_with_libreoffice(
                pptx_path,
                work,
                dpi=dpi,
            )

            if images:
                return (
                    images,
                    "LibreOffice + Poppler",
                )

        except Exception as exc:
            errors.append(
                f"LibreOffice: {exc}"
            )

        raise PowerPointRenderError(
            "Không render được PowerPoint. "
            + " | ".join(errors)
        )