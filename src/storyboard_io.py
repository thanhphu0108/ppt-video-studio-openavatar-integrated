from __future__ import annotations

import io
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd


IMPORT_COLUMNS = (
    "Slide",
    "Tiêu đề",
    "Lời thuyết minh",
    "Xuất",
    "Nghỉ sau (giây)",
)


@dataclass(frozen=True)
class StoryboardUpdate:
    """Các trường có thể thay đổi của một slide khi nhập storyboard."""

    slide: int
    title: str | None = None
    narration: str | None = None
    export: bool | None = None
    pause_after: float | None = None


def _column_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", normalized.lower().replace("đ", "d"))


def _is_empty(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return False
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _text(value: object) -> str:
    return "" if _is_empty(value) else str(value).strip()


def _find_column(frame: pd.DataFrame, *aliases: str) -> str | None:
    indexed = {_column_key(column): str(column) for column in frame.columns}
    for alias in aliases:
        found = indexed.get(_column_key(alias))
        if found is not None:
            return found
    return None


def _parse_slide(value: object, row_number: int) -> int:
    if _is_empty(value):
        raise ValueError(f"Dòng {row_number}: thiếu cột Slide.")
    try:
        numeric = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Dòng {row_number}: Slide phải là số nguyên dương.") from exc
    if not numeric.is_integer() or numeric < 1:
        raise ValueError(f"Dòng {row_number}: Slide phải là số nguyên dương.")
    return int(numeric)


def _parse_bool(value: object, row_number: int, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    token = _column_key(value)
    truthy = {"1", "true", "yes", "y", "x", "co", "cothu", "include", "export", "xuat"}
    falsy = {"0", "false", "no", "n", "khong", "bo", "skip", "exclude"}
    if token in truthy:
        return True
    if token in falsy:
        return False
    raise ValueError(
        f"Dòng {row_number}: cột {label} chỉ nhận Có/Không, TRUE/FALSE hoặc 1/0."
    )


def _parse_pause(value: object, row_number: int) -> float:
    if _is_empty(value):
        raise ValueError(f"Dòng {row_number}: Nghỉ sau (giây) không được để trống.")
    try:
        seconds = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Dòng {row_number}: Nghỉ sau (giây) phải là một số.") from exc
    if not 0 <= seconds <= 60:
        raise ValueError(f"Dòng {row_number}: Nghỉ sau (giây) phải trong khoảng 0–60.")
    return seconds


def storyboard_dataframe(records: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Tạo file mẫu có sẵn các slide của PowerPoint đang làm việc."""

    return pd.DataFrame(
        [
            {
                "Slide": int(record["slide"]),
                "Tiêu đề": str(record.get("title", "")),
                "Lời thuyết minh": str(record.get("narration", "")),
                "Xuất": not bool(record.get("skip", False)),
                "Nghỉ sau (giây)": float(record.get("pause_after", 0.35)),
            }
            for record in records
        ],
        columns=IMPORT_COLUMNS,
    )


def sample_storyboard_dataframe() -> pd.DataFrame:
    """Mẫu minh hoạ dùng khi chưa tải PowerPoint."""

    return pd.DataFrame(
        [
            {
                "Slide": 1,
                "Tiêu đề": "Tiêu đề slide",
                "Lời thuyết minh": "Nhập lời thuyết minh cho slide tại đây.",
                "Xuất": True,
                "Nghỉ sau (giây)": 0.35,
            }
        ],
        columns=IMPORT_COLUMNS,
    )


def storyboard_csv_bytes(records: Sequence[Mapping[str, Any]] | None = None) -> bytes:
    frame = storyboard_dataframe(records) if records else sample_storyboard_dataframe()
    return frame.to_csv(index=False).encode("utf-8-sig")


def storyboard_xlsx_bytes(records: Sequence[Mapping[str, Any]] | None = None) -> bytes:
    frame = storyboard_dataframe(records) if records else sample_storyboard_dataframe()
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Storyboard")
        worksheet = writer.sheets["Storyboard"]
        worksheet.freeze_panes = "A2"
        for column in worksheet.columns:
            width = max(len(str(cell.value or "")) for cell in column) + 2
            worksheet.column_dimensions[column[0].column_letter].width = min(max(width, 14), 64)
    return buffer.getvalue()


def read_storyboard_upload(payload: bytes, filename: str) -> pd.DataFrame:
    """Đọc CSV, XLSX hoặc JSON; hỗ trợ luôn project JSON của ứng dụng."""

    suffix = Path(filename or "").suffix.lower()
    if suffix in {".csv", ".tsv"}:
        separator = "\t" if suffix == ".tsv" else None
        for encoding in ("utf-8-sig", "utf-8", "cp1258"):
            try:
                return pd.read_csv(io.BytesIO(payload), encoding=encoding, sep=separator, engine="python")
            except UnicodeDecodeError:
                continue
        raise ValueError("Không đọc được mã hóa của file CSV.")
    if suffix in {".xlsx", ".xls"}:
        try:
            return pd.read_excel(io.BytesIO(payload))
        except ImportError as exc:
            raise ValueError("Cần cài openpyxl để nhập Excel.") from exc
    if suffix == ".json":
        try:
            parsed = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON không hợp lệ: {exc}") from exc
        if isinstance(parsed, dict):
            parsed = parsed.get("storyboard", parsed.get("slides", parsed))
        if not isinstance(parsed, list):
            raise ValueError("JSON phải là mảng storyboard hoặc project có trường storyboard.")
        return pd.DataFrame(parsed)
    raise ValueError("Chỉ hỗ trợ file CSV, XLSX, XLS hoặc JSON.")


def prepare_storyboard_updates(
    records: Sequence[Mapping[str, Any]], imported: pd.DataFrame
) -> list[StoryboardUpdate]:
    """Kiểm tra toàn bộ file trước khi trả về các cập nhật để app áp dụng một lần."""

    if imported.empty:
        raise ValueError("File import không có dòng dữ liệu nào.")

    slide_column = _find_column(imported, "Slide", "slide_number", "source_slide_number", "stt")
    if slide_column is None:
        raise ValueError("File import cần có cột Slide.")
    title_column = _find_column(imported, "Tiêu đề", "title")
    narration_column = _find_column(imported, "Lời thuyết minh", "narration", "Lời đọc", "script")
    export_column = _find_column(imported, "Xuất", "export", "include")
    skip_column = _find_column(imported, "skip", "Bỏ qua")
    pause_column = _find_column(imported, "Nghỉ sau (giây)", "pause_after", "pause")

    editable = [title_column, narration_column, export_column, skip_column, pause_column]
    if not any(editable):
        raise ValueError(
            "File import chưa có trường nào có thể cập nhật: Tiêu đề, Lời thuyết minh, Xuất hoặc Nghỉ sau (giây)."
        )

    existing_slides = {int(record["slide"]) for record in records}
    seen: set[int] = set()
    updates: list[StoryboardUpdate] = []
    errors: list[str] = []

    for index, row in imported.iterrows():
        row_number = int(index) + 2
        try:
            slide = _parse_slide(row[slide_column], row_number)
            if slide not in existing_slides:
                raise ValueError(f"Dòng {row_number}: không có Slide {slide} trong PowerPoint hiện tại.")
            if slide in seen:
                raise ValueError(f"Dòng {row_number}: Slide {slide} bị lặp trong file import.")
            seen.add(slide)

            title = _text(row[title_column]) if title_column is not None else None
            narration = _text(row[narration_column]) if narration_column is not None else None
            export = (
                _parse_bool(row[export_column], row_number, "Xuất")
                if export_column is not None
                else None
            )
            if skip_column is not None:
                export = not _parse_bool(row[skip_column], row_number, "Bỏ qua")
            pause_after = (
                _parse_pause(row[pause_column], row_number)
                if pause_column is not None
                else None
            )
            updates.append(
                StoryboardUpdate(
                    slide=slide,
                    title=title,
                    narration=narration,
                    export=export,
                    pause_after=pause_after,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError("\n".join(errors[:12]))
    return updates


def apply_storyboard_updates(
    records: list[dict[str, Any]], updates: Sequence[StoryboardUpdate]
) -> int:
    """Áp dụng những cập nhật đã được kiểm tra; trả về số slide thay đổi."""

    by_slide = {int(record["slide"]): record for record in records}
    changed = 0
    for update in updates:
        record = by_slide[update.slide]
        before = (
            record.get("title", ""),
            record.get("narration", ""),
            not bool(record.get("skip", False)),
            float(record.get("pause_after", 0.35)),
        )
        if update.title is not None:
            record["title"] = update.title
        if update.narration is not None:
            record["narration"] = update.narration
        if update.export is not None:
            record["skip"] = not update.export
        if update.pause_after is not None:
            record["pause_after"] = update.pause_after
        after = (
            record.get("title", ""),
            record.get("narration", ""),
            not bool(record.get("skip", False)),
            float(record.get("pause_after", 0.35)),
        )
        if before != after:
            changed += 1
    return changed
