import json

import pandas as pd
import pytest

from src.storyboard_io import (
    apply_storyboard_updates,
    prepare_storyboard_updates,
    read_storyboard_upload,
    storyboard_csv_bytes,
    storyboard_xlsx_bytes,
)


def _records():
    return [
        {
            "slide": 1,
            "title": "Mở đầu",
            "narration": "Lời đọc cũ một.",
            "skip": False,
            "pause_after": 0.35,
        },
        {
            "slide": 2,
            "title": "Kết quả",
            "narration": "Lời đọc cũ hai.",
            "skip": False,
            "pause_after": 0.35,
        },
    ]


def test_csv_template_can_be_imported_and_applied():
    records = _records()
    frame = read_storyboard_upload(storyboard_csv_bytes(records), "storyboard_mau.csv")
    frame.loc[0, "Lời thuyết minh"] = "Lời đọc mới một."
    frame["Xuất"] = frame["Xuất"].astype(object)
    frame["Nghỉ sau (giây)"] = frame["Nghỉ sau (giây)"].astype(object)
    frame.loc[1, "Xuất"] = "Không"
    frame.loc[1, "Nghỉ sau (giây)"] = "1,25"

    updates = prepare_storyboard_updates(records, frame)
    changed = apply_storyboard_updates(records, updates)

    assert changed == 2
    assert records[0]["narration"] == "Lời đọc mới một."
    assert records[1]["skip"] is True
    assert records[1]["pause_after"] == 1.25


def test_project_json_is_accepted_for_storyboard_import():
    payload = json.dumps(
        {
            "project_version": 1,
            "storyboard": [
                {"slide": 1, "title": "Mở đầu mới", "narration": "Xin chào", "skip": False, "pause_after": 0.5}
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    frame = read_storyboard_upload(payload, "project.json")
    updates = prepare_storyboard_updates(_records(), frame)

    assert updates[0].slide == 1
    assert updates[0].title == "Mở đầu mới"
    assert updates[0].export is True
    assert updates[0].pause_after == 0.5


def test_excel_template_can_be_read_back():
    frame = read_storyboard_upload(storyboard_xlsx_bytes(_records()), "storyboard_mau.xlsx")

    assert list(frame["Slide"]) == [1, 2]
    assert list(frame["Tiêu đề"]) == ["Mở đầu", "Kết quả"]


def test_import_rejects_unknown_or_duplicate_slide():
    unknown = pd.DataFrame({"Slide": [3], "Lời thuyết minh": ["Không tồn tại"]})
    with pytest.raises(ValueError, match="không có Slide 3"):
        prepare_storyboard_updates(_records(), unknown)

    duplicate = pd.DataFrame({"Slide": [1, 1], "Lời thuyết minh": ["A", "B"]})
    with pytest.raises(ValueError, match="bị lặp"):
        prepare_storyboard_updates(_records(), duplicate)


def test_missing_cells_from_excel_are_handled_as_empty_values():
    imported = pd.DataFrame({"Slide": [1], "Lời thuyết minh": [pd.NA]})

    updates = prepare_storyboard_updates(_records(), imported)

    assert updates[0].narration == ""
