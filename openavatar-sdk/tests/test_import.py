import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "python"))

from openavatar_sdk import OpenAvatarClient

def test_client():
    assert OpenAvatarClient().base_url == "http://127.0.0.1:8008"
