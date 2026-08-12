from __future__ import annotations

import hashlib
import hmac


# Mật khẩu mặc định được lưu ở dạng khóa dẫn xuất PBKDF2, không lưu dạng rõ.
_DEFAULT_SALT = bytes.fromhex("b9713276e9114ffb8da0ec76b0a58057")
_DEFAULT_DERIVED_KEY = bytes.fromhex(
    "4fda0d19b2c9f07bd40d102ab82a7f69968ac1c6b89b21ea5842436b27d77358"
)
_PBKDF2_ITERATIONS = 210_000


def derive_password_key(password: str) -> bytes:
    """Tạo khóa kiểm tra ổn định từ mật khẩu mà không giữ lại mật khẩu gốc."""

    return hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        _DEFAULT_SALT,
        _PBKDF2_ITERATIONS,
    )


def verify_voice_upload_password(
    password: str,
    *,
    configured_password: str | None = None,
) -> bool:
    """Kiểm tra mật khẩu mở khóa các luồng có tải audio giọng người dùng.

    `configured_password` cho phép triển khai thay đổi mật khẩu qua environment
    hoặc Streamlit secrets mà không cần sửa source. Khi không cấu hình, dùng
    mật khẩu mặc định đã được cung cấp cho dự án.
    """

    if configured_password:
        return hmac.compare_digest(str(password or ""), configured_password)
    return hmac.compare_digest(derive_password_key(password), _DEFAULT_DERIVED_KEY)
