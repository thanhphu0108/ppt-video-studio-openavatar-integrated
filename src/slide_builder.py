from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _wrap(draw, text, font, width):
    words = str(text).split()
    lines, current = [], ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_content_slide(title: str, bullets: list[str], organization: str = "", role: str = "content") -> Image.Image:
    img = Image.new("RGB", (1280, 720), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 1280, 84), fill=(30, 64, 175))
    draw.text((52, 24), organization or "PPT Video Studio", font=_font(25, True), fill="white")
    y = 122
    for line in _wrap(draw, title, _font(43, True), 1160)[:3]:
        draw.text((58, y), line, font=_font(43, True), fill=(15, 23, 42))
        y += 58
    y += 20
    for bullet in bullets[:5]:
        draw.ellipse((62, y + 10, 74, y + 22), fill=(30, 64, 175))
        for line in _wrap(draw, bullet, _font(25), 1080)[:3]:
            draw.text((92, y), line, font=_font(25), fill=(30, 41, 59))
            y += 36
        y += 14
    draw.text((56, 680), "Được tạo bằng PPT Video Studio", font=_font(15), fill=(100, 116, 139))
    return img


def build_boundary_slide(title: str, subtitle: str, organization: str = "", role: str = "intro") -> Image.Image:
    img = Image.new("RGB", (1280, 720), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 22, 720), fill=(37, 99, 235))
    draw.text((74, 76), organization or "PPT VIDEO STUDIO", font=_font(24, True), fill=(191, 219, 254))
    y = 235
    for line in _wrap(draw, title, _font(55, True), 1080)[:4]:
        draw.text((74, y), line, font=_font(55, True), fill="white")
        y += 72
    if subtitle:
        draw.text((78, min(y + 20, 590)), subtitle, font=_font(27), fill=(203, 213, 225))
    return img
