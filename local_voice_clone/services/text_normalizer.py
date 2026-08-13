from __future__ import annotations

import re


_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]


def _read_two_digits(value: int, *, full: bool = False) -> str:
    tens, units = divmod(value, 10)
    if tens == 0:
        return (_DIGITS[units] if full else "") if units else ""
    if tens == 1:
        return "mười" if units == 0 else f"mười {'lăm' if units == 5 else _DIGITS[units]}"
    tail = ""
    if units == 1:
        tail = " mốt"
    elif units == 4 and tens > 1:
        tail = " tư"
    elif units == 5:
        tail = " lăm"
    elif units:
        tail = f" {_DIGITS[units]}"
    return f"{_DIGITS[tens]} mươi{tail}"


def number_to_vietnamese(value: int) -> str:
    """Đọc số nguyên không âm cho các số báo cáo thông thường."""

    if value == 0:
        return _DIGITS[0]
    if value < 0:
        return "âm " + number_to_vietnamese(-value)
    units = [(1_000_000_000, "tỷ"), (1_000_000, "triệu"), (1_000, "nghìn")]
    parts: list[str] = []
    remaining = value
    for divisor, label in units:
        amount, remaining = divmod(remaining, divisor)
        if amount:
            parts.append(f"{number_to_vietnamese(amount)} {label}")
    if remaining:
        hundreds, rest = divmod(remaining, 100)
        if hundreds:
            parts.append(f"{_DIGITS[hundreds]} trăm")
        if rest:
            if hundreds and rest < 10:
                parts.append("lẻ " + _read_two_digits(rest, full=True))
            elif rest < 10:
                # Sau nghìn/triệu, phần lẻ vẫn phải được đọc (1.001 ->
                # "một nghìn một"), còn số một chữ số độc lập cũng vậy.
                parts.append(_DIGITS[rest])
            else:
                parts.append(_read_two_digits(rest, full=bool(hundreds or parts)))
    return " ".join(part for part in parts if part).strip()


def _replace_decimal(match: re.Match[str]) -> str:
    whole = number_to_vietnamese(int(match.group(1)))
    fraction = " ".join(_DIGITS[int(char)] for char in match.group(2))
    return f"{whole} phẩy {fraction}"


def _replace_integer(match: re.Match[str]) -> str:
    token = match.group(0)
    try:
        return number_to_vietnamese(int(token))
    except ValueError:
        return token


class VietnameseTextNormalizer:
    def __init__(self, *, normalize_numbers: bool = True) -> None:
        self.normalize_numbers = normalize_numbers

    def normalize(self, text: str) -> str:
        normalized = str(text or "").replace("\ufeff", " ").replace("\u200b", " ")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"[\t ]+", " ", normalized)
        # Preserve a real paragraph separator for chunking/pause selection,
        # while treating normal line wraps as spaces.
        normalized = re.sub(r"\n[ ]*\n+", "\n\n", normalized)
        normalized = re.sub(r"(?<!\n)\n(?!\n)", " ", normalized)
        if self.normalize_numbers:
            # Process numbers before spacing punctuation, otherwise `98,65%`
            # becomes `98, 65%` and is incorrectly treated as two integers.
            normalized = re.sub(
                r"(?i)\bngày\s+(\d{1,2})/(\d{1,2})/(\d{4})\b",
                lambda match: (
                    f"ngày {number_to_vietnamese(int(match.group(1)))} "
                    f"tháng {number_to_vietnamese(int(match.group(2)))} "
                    f"năm {number_to_vietnamese(int(match.group(3)))}"
                ),
                normalized,
            )
            normalized = re.sub(
                r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
                lambda match: (
                    f"ngày {number_to_vietnamese(int(match.group(1)))} "
                    f"tháng {number_to_vietnamese(int(match.group(2)))} "
                    f"năm {number_to_vietnamese(int(match.group(3)))}"
                ),
                normalized,
            )
            normalized = re.sub(r"\b(\d+)[,.](\d+)\s*%", lambda m: _replace_decimal(m) + " phần trăm", normalized)
            normalized = re.sub(r"\b(\d+)\s*%", lambda m: number_to_vietnamese(int(m.group(1))) + " phần trăm", normalized)
            normalized = re.sub(r"\b(\d+)[,.](\d+)\b", _replace_decimal, normalized)
            normalized = re.sub(r"\b\d+\b", _replace_integer, normalized)
        normalized = re.sub(r"[ ]*([,.;:!?])[ ]*", r"\1 ", normalized)
        normalized = re.sub(r" *\n\n *", "\n\n", normalized)
        normalized = re.sub(r" {2,}", " ", normalized).strip()
        return normalized


def chunk_text(text: str, max_chars: int) -> list[tuple[str, bool]]:
    """Tách theo paragraph/câu; bool đánh dấu ngắt paragraph sau đoạn."""

    paragraphs = [segment.strip() for segment in re.split(r"\n\s*\n+", text) if segment.strip()]
    if not paragraphs:
        return []
    chunks: list[tuple[str, bool]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        sentences = [item.strip() for item in re.split(r"(?<=[.!?;:])\s+", paragraph) if item.strip()]
        current = ""
        for sentence in sentences or [paragraph]:
            if len(sentence) > max_chars:
                words = sentence.split()
                for word in words:
                    candidate = word if not current else f"{current} {word}"
                    if len(candidate) > max_chars and current:
                        chunks.append((current, False))
                        current = word
                    else:
                        current = candidate
                continue
            candidate = sentence if not current else f"{current} {sentence}"
            if len(candidate) > max_chars and current:
                chunks.append((current, False))
                current = sentence
            else:
                current = candidate
        if current:
            chunks.append((current, paragraph_index < len(paragraphs) - 1))
    return chunks
