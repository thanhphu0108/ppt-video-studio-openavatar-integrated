from __future__ import annotations

import shutil
import subprocess
import tempfile
import os
from pathlib import Path

from PIL import Image


class PowerPointRenderError(RuntimeError):
    pass


def _find_command(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path

    # Windows installers thường không thêm LibreOffice/Poppler vào PATH.
    # Tìm thêm các vị trí cài đặt mặc định để app local không báo thiếu
    # dù chương trình đã được cài trên máy.
    if os.name == "nt":
        program_roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]

        for name in names:
            lowered = name.lower()
            candidates: list[Path] = []

            if lowered in {"libreoffice", "soffice"}:
                for root in program_roots:
                    if root:
                        candidates.append(
                            Path(root) / "LibreOffice" / "program" / "soffice.exe"
                        )
            elif lowered == "pdftoppm":
                for root in program_roots[:2]:
                    if root:
                        base = Path(root)
                        candidates.extend(
                            [
                                base / "poppler" / "Library" / "bin" / "pdftoppm.exe",
                                base / "poppler" / "bin" / "pdftoppm.exe",
                            ]
                        )
                        candidates.extend(
                            base.glob("poppler*/Library/bin/pdftoppm.exe")
                        )

            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)

    return None


def _render_with_powerpoint_com(
    pptx_path: Path,
    work: Path,
) -> list[Image.Image]:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise PowerPointRenderError(
            "Chưa cài pywin32 để dùng Microsoft PowerPoint COM."
        ) from exc

    export_dir = work / "ppt_export"
    export_dir.mkdir(parents=True, exist_ok=True)

    app = None
    presentation = None
    com_initialized = False

    try:
        # Streamlit chạy mỗi lần xử lý trong một thread riêng. COM không tự
        # được khởi tạo trong thread đó, nên DispatchEx có thể lỗi:
        # "CoInitialize has not been called".
        pythoncom.CoInitialize()
        com_initialized = True

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

        if com_initialized:
            try:
                pythoncom.CoUninitialize()
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

    scale = max(72, int(dpi))

    if pdftoppm:
        prefix = work / "slide"

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

    # PyMuPDF đã là dependency của app, nên dùng làm fallback local khi
    # Poppler chưa được thêm vào PATH. LibreOffice vẫn giữ nguyên bố cục;
    # chỉ thay cách rasterize PDF thành PNG.
    try:
        import fitz
    except ImportError as exc:
        raise PowerPointRenderError(
            "Không tìm thấy pdftoppm và chưa cài PyMuPDF để rasterize PDF."
        ) from exc

    try:
        pdf = fitz.open(str(pdf_path))
        matrix = fitz.Matrix(scale / 72.0, scale / 72.0)
        images = []
        for page in pdf:
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(
                Image.frombytes(
                    "RGB",
                    [pixmap.width, pixmap.height],
                    pixmap.samples,
                )
            )
        pdf.close()
    except Exception as exc:
        raise PowerPointRenderError(
            f"Không chuyển được PDF sang ảnh bằng PyMuPDF: {exc}"
        ) from exc

    if not images:
        raise PowerPointRenderError(
            "LibreOffice tạo PDF nhưng PDF không có trang slide."
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
