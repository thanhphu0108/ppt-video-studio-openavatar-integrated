from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


class CacheService:
    def __init__(self, cache_dir: Path, *, enabled: bool = True) -> None:
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def hash_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1_048_576), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def build_key(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _audio_path(self, key: str, output_format: str) -> Path:
        return self.cache_dir / f"{key}.{output_format}"

    def _metadata_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def restore(self, key: str, output_format: str, destination: str | Path) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        cached = self._audio_path(key, output_format)
        metadata_path = self._metadata_path(key)
        if not cached.exists() or cached.stat().st_size == 0 or not metadata_path.exists():
            return None
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, target)
        try:
            return json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def store(self, key: str, output_format: str, source: str | Path, metadata: dict[str, Any]) -> None:
        if not self.enabled:
            return
        source_path = Path(source)
        if not source_path.exists() or source_path.stat().st_size == 0:
            return
        shutil.copyfile(source_path, self._audio_path(key, output_format))
        self._metadata_path(key).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
