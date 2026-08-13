from __future__ import annotations

import hashlib
import hmac
import os

from .errors import VoiceCloneServiceError


# The default is represented as a PBKDF2-derived key only. It matches the
# protected upload flow in the parent PPT application without keeping the
# password in cleartext in either project.
_DEFAULT_SALT = bytes.fromhex("b9713276e9114ffb8da0ec76b0a58057")
_DEFAULT_DERIVED_KEY = bytes.fromhex(
    "4fda0d19b2c9f07bd40d102ab82a7f69968ac1c6b89b21ea5842436b27d77358"
)
_PBKDF2_ITERATIONS = 210_000


def _derived_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        _DEFAULT_SALT,
        _PBKDF2_ITERATIONS,
    )


def verify_upload_password(password: str, configured_password: str | None = None) -> bool:
    """Verify without persisting or logging a plaintext voice-upload password."""

    configured = configured_password if configured_password is not None else os.getenv("VOICE_UPLOAD_PASSWORD", "")
    if configured:
        return hmac.compare_digest(str(password or ""), configured)
    return hmac.compare_digest(_derived_key(password), _DEFAULT_DERIVED_KEY)


def enforce_uploaded_voice_consent(*, consent: bool, password: str, required: bool = True) -> None:
    if not consent:
        raise VoiceCloneServiceError(
            "VOICE_USE_CONSENT_REQUIRED",
            "Cần xác nhận bạn có quyền sử dụng giọng nói mẫu trước khi nhân bản.",
            status_code=403,
        )
    if required and not verify_upload_password(password):
        raise VoiceCloneServiceError(
            "VOICE_UPLOAD_PASSWORD_INVALID",
            "Mật khẩu dùng giọng tải lên không đúng.",
            status_code=401,
        )
