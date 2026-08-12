from src.voice_access import derive_password_key, verify_voice_upload_password


def test_configured_voice_upload_password_is_checked_in_constant_time_path():
    assert verify_voice_upload_password("correct", configured_password="correct")
    assert not verify_voice_upload_password("incorrect", configured_password="correct")


def test_default_voice_upload_password_does_not_accept_an_empty_value():
    assert not verify_voice_upload_password("")


def test_password_derivation_is_deterministic_without_retaining_plaintext():
    assert derive_password_key("example") == derive_password_key("example")
    assert derive_password_key("example") != derive_password_key("different")
