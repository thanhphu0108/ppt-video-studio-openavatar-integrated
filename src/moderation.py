from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def normalize_for_moderation(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).lower().translate(LEET_MAP)
    return re.sub(r"[^0-9a-zà-ỹđ]+", "", text, flags=re.IGNORECASE)


class ProfanityFilter:
    def __init__(self, config_path: str | Path):
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.blocked_exact = {normalize_for_moderation(v) for v in data.get("blocked_exact", [])}
        self.blocked_contains = {normalize_for_moderation(v) for v in data.get("blocked_contains", [])}
        self.exceptions = {normalize_for_moderation(v) for v in data.get("exceptions", [])}

    def contains_profanity(self, value: str) -> bool:
        normalized = normalize_for_moderation(value)
        if not normalized or normalized in self.exceptions:
            return False
        if normalized in self.blocked_exact:
            return True
        return any(term and term in normalized for term in self.blocked_contains)

    def validate(self, value: str, field_name: str = "Nội dung") -> list[str]:
        errors: list[str] = []
        raw = str(value or "").strip()
        if not raw:
            errors.append(f"{field_name} không được để trống.")
        if len(raw) > 300:
            errors.append(f"{field_name} vượt quá 300 ký tự.")
        if re.search(r"https?://|<\s*script|javascript:", raw, flags=re.IGNORECASE):
            errors.append(f"{field_name} chứa URL hoặc mã không được phép.")
        if self.contains_profanity(raw):
            errors.append(f"{field_name} chứa từ ngữ không phù hợp và không thể lưu.")
        return errors
