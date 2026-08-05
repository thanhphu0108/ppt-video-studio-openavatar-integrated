from src.models import PronunciationEntry
from src.pronunciation import apply_dictionary


def test_apply_dictionary_whole_word():
    entries = [PronunciationEntry("BHYT", "bảo hiểm y tế", case_sensitive=True)]
    assert apply_dictionary("Hồ sơ BHYT", entries) == "Hồ sơ bảo hiểm y tế"
