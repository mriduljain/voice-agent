"""Small, dependency-free PCM and G.711 conversion helpers.

This module deliberately has no FFmpeg or intermediate-file dependency. It
uses bytes and in-memory chunks so a carrier or the ADK Live model can stream.
"""

from __future__ import annotations

import struct


def mulaw_decode_byte(value: int) -> int:
    """Decode one G.711 mu-law byte to a signed 16-bit PCM sample."""
    inverted = (~value) & 0xFF
    sign = -1 if value & 0x80 else 1
    exponent = (inverted >> 4) & 0x07
    mantissa = inverted & 0x0F
    magnitude = ((33 + mantissa) << exponent) - 33
    return max(-32768, min(32767, sign * magnitude))


_MULAW_DECODE_TABLE = tuple(mulaw_decode_byte(value) for value in range(256))


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Encode signed little-endian 16-bit PCM as G.711 mu-law bytes."""
    values = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm):
        sign = 0x80 if sample >= 0 else 0x00
        magnitude = abs(sample)
        candidates = (
            value for value in range(256)
            if (value & 0x80) == sign
        )
        value = min(candidates, key=lambda code: abs(_MULAW_DECODE_TABLE[code] - magnitude))
        values.append(value)
    return bytes(values)


def mulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode G.711 mu-law bytes to signed little-endian 16-bit PCM."""
    return struct.pack("<" + "h" * len(payload), *(_MULAW_DECODE_TABLE[value] for value in payload))


class LinearResampler:
    """Source-position PCM resampler for in-memory chunks.

    It retains state across chunks, using the source position to calculate
    interpolated samples.
    """

    def __init__(self, input_rate: int, output_rate: int) -> None:
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("Audio rates must be positive")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._next_position = 0.0
        self._input_position = 0.0
        self._previous: int | None = None

    def resample(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM16 input length must be even")

        samples = [sample for (sample,) in struct.iter_unpack("<h", pcm)]
        if not samples:
            return b""

        start_position = self._input_position
        end_position = start_position + len(samples)
        step = self.input_rate / self.output_rate
        output: list[int] = []

        while self._next_position < end_position:
            position = self._next_position
            lower = int(position)
            fraction = position - lower

            if lower < start_position:
                left = self._previous if self._previous is not None else samples[0]
            elif lower < end_position:
                left = samples[lower - int(start_position)]
            else:
                break

            right_index = lower + 1
            if right_index < start_position:
                right = left
            elif right_index < end_position:
                right = samples[right_index - int(start_position)]
            else:
                right = left

            interpolated = round(left + (right - left) * fraction)
            output.append(max(-32768, min(32767, interpolated)))
            self._next_position += step

        self._previous = samples[-1]
        self._input_position = end_position
        return struct.pack("<" + "h" * len(output), *output)

    def reset(self) -> None:
        self._next_position = 0.0
        self._input_position = 0.0
        self._previous = None
