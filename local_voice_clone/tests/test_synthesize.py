from __future__ import annotations

from config.settings import get_settings
from services.synthesis_service import SynthesisService
from services.text_normalizer import VietnameseTextNormalizer, chunk_text


def test_vietnamese_normalization_and_paragraph_chunking() -> None:
    normalized = VietnameseTextNormalizer(normalize_numbers=True).normalize(
        "Tỷ lệ 98,65%.\n\nNgày 01/02/2026 bắt đầu."
    )
    assert "chín mươi tám phẩy sáu năm phần trăm" in normalized
    assert "ngày một tháng hai năm" in normalized
    chunks = chunk_text(normalized, 40)
    assert chunks
    assert any(is_paragraph for _, is_paragraph in chunks)


def test_dummy_synthesizes_vietnamese_wav_and_hits_cache(local_env, reference_wav) -> None:
    service = SynthesisService(get_settings())
    first = service.synthesize(
        model="dummy",
        text="Kính thưa quý anh chị, hôm nay chúng ta cùng trao đổi về trải nghiệm người bệnh.",
        reference_audio=reference_wav,
        reference_text="Xin chào quý anh chị, đây là mẫu giọng dùng để tổng hợp tiếng nói.",
        output_format="wav",
    )
    second = service.synthesize(
        model="dummy",
        text="Kính thưa quý anh chị, hôm nay chúng ta cùng trao đổi về trải nghiệm người bệnh.",
        reference_audio=reference_wav,
        reference_text="Xin chào quý anh chị, đây là mẫu giọng dùng để tổng hợp tiếng nói.",
        output_format="wav",
    )
    assert first.audio_path.exists()
    assert first.audio_path.stat().st_size > 512
    assert first.duration_seconds > 0
    assert second.cache_hit is True

