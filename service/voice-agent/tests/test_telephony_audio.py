import struct

from app.telephony_audio import (
    LinearResampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)


def test_mulaw_round_trip_preserves_pcm_shape():
    pcm = struct.pack("<4h", -32768, -1, 0, 32767)

    encoded = pcm16_to_mulaw(pcm)
    decoded = mulaw_to_pcm16(encoded)

    assert len(encoded) == 4
    assert len(decoded) == 8


def test_resampler_accepts_multiple_even_length_chunks():
    resampler = LinearResampler(input_rate=8000, output_rate=16000)

    first = struct.pack("<4h", 0, 1000, 2000, 3000)
    second = struct.pack("<4h", 4000, 5000, 6000, 7000)

    assert len(resampler.resample(first)) % 2 == 0
    assert len(resampler.resample(second)) % 2 == 0


def test_resampler_rejects_odd_pcm_length():
    resampler = LinearResampler(input_rate=8000, output_rate=16000)

    try:
        resampler.resample(b"\x00")
    except ValueError:
        return
    raise AssertionError("Expected ValueError for odd PCM byte length")
