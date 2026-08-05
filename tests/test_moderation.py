from pathlib import Path
from src.moderation import ProfanityFilter


def test_filter_blocks_obfuscated_word():
    root = Path(__file__).parents[1]
    f = ProfanityFilter(root / "config" / "profanity_vi.json")
    assert f.contains_profanity("d.m")
    assert not f.contains_profanity("buổi đào tạo")
