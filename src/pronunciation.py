from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import PronunciationEntry
from .moderation import ProfanityFilter


def load_dictionary(path: str | Path) -> list[PronunciationEntry]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [PronunciationEntry(**entry) for entry in data.get("entries", [])]


def parse_dictionary_bytes(payload: bytes) -> list[PronunciationEntry]:
    data = json.loads(payload.decode("utf-8-sig"))
    return [PronunciationEntry(**entry) for entry in data.get("entries", [])]


def dictionary_json(entries: list[PronunciationEntry]) -> str:
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entries": [asdict(entry) for entry in entries],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def validate_entry(entry: PronunciationEntry, existing: list[PronunciationEntry], profanity: ProfanityFilter) -> list[str]:
    errors = profanity.validate(entry.source, "Từ gốc") + profanity.validate(entry.pronunciation, "Cách đọc")
    if entry.source.strip().casefold() == entry.pronunciation.strip().casefold():
        errors.append("Từ gốc và cách đọc không được giống nhau.")
    for item in existing:
        if item.source.strip().casefold() == entry.source.strip().casefold():
            errors.append("Từ gốc đã tồn tại trong từ điển.")
            break
    reverse_map = {item.pronunciation.strip().casefold(): item.source.strip().casefold() for item in existing}
    if reverse_map.get(entry.source.strip().casefold()) == entry.pronunciation.strip().casefold():
        errors.append("Ánh xạ này tạo vòng lặp phát âm.")
    return list(dict.fromkeys(errors))


def apply_dictionary(text: str, entries: list[PronunciationEntry]) -> str:
    result = str(text or "")
    for entry in sorted((e for e in entries if e.enabled), key=lambda e: len(e.source), reverse=True):
        pattern = re.escape(entry.source)
        if entry.whole_word:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        flags = 0 if entry.case_sensitive else re.IGNORECASE
        result = re.sub(pattern, entry.pronunciation, result, flags=flags)
    return result
