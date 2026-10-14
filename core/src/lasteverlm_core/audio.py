"""What an audio upload is, before it is sent to be transcribed.

The transcription model accepts WAV and MP3 only, at most 25 MiB and
four hours. `probe_audio` reads the format from the bytes (not the file
name), measures the duration from the file itself and refuses anything
outside those limits with `AudioError`, so a call the platform would
reject (and perhaps bill) is never made. Pure Python: WAV from its
`fmt ` and `data` chunks, MP3 by walking every frame header, which is
exact for constant and variable bit rates alike.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

AudioFormat = Literal["wav", "mp3"]

# The platform's limits for the transcription model (GET /v1/models:
# max_content_size_bytes, max_duration_seconds).
MAX_AUDIO_BYTES = 26_214_400
MAX_AUDIO_SECONDS = 14_400

MIME_TYPES: dict[AudioFormat, str] = {"wav": "audio/wav", "mp3": "audio/mpeg"}

# Recognised only to name them in the refusal.
_OTHER_SIGNATURES = (
    (0, b"OggS", "an Ogg file"),
    (0, b"fLaC", "a FLAC file"),
    (4, b"ftyp", "an MP4/M4A file"),
    (0, b"\x1aE\xdf\xa3", "a WebM/Matroska file"),
)

# How far into the file (after an ID3v2 tag) the first MP3 frame may start.
_MP3_SYNC_WINDOW = 64 * 1024


class AudioError(ValueError):
    """The upload is not audio the transcription model takes. The message is
    meant for the user: it names the format or the limit."""


@dataclass(frozen=True)
class AudioInfo:
    """The detected format, its MIME type and the measured duration in seconds."""

    format: AudioFormat
    mime: str
    seconds: float


def probe_audio(data: bytes) -> AudioInfo:
    """Detect, measure and check one upload; `AudioError` when it cannot be sent."""
    if not data:
        raise AudioError("the audio file is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError(
            f"the audio file is {len(data)} bytes; at most {MAX_AUDIO_BYTES} (25 MiB) "
            "can be transcribed"
        )
    info = measure_audio(data)
    if info.seconds > MAX_AUDIO_SECONDS:
        raise AudioError(
            f"the audio is {math.ceil(info.seconds)} seconds long; at most {MAX_AUDIO_SECONDS} "
            "(4 hours) can be transcribed"
        )
    return info


def measure_audio(data: bytes) -> AudioInfo:
    """Format and duration of WAV or MP3 bytes, without the transcription
    limits (it also measures synthesized speech); `AudioError` otherwise."""
    if not data:
        raise AudioError("the audio file is empty")
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        fmt: AudioFormat = "wav"
        seconds = _wav_seconds(data)
    else:
        for offset, magic, name in _OTHER_SIGNATURES:
            if data[offset : offset + len(magic)] == magic:
                raise AudioError(f"this is {name}; only WAV and MP3 can be transcribed")
        fmt = "mp3"
        seconds = _mp3_seconds(data)
    if seconds <= 0:
        raise AudioError("the audio file contains no audio")
    return AudioInfo(fmt, MIME_TYPES[fmt], seconds)


def billed_seconds(seconds: float) -> int:
    """Whole seconds, rounded up: the unit `stt` usage is priced in."""
    return max(1, math.ceil(seconds))


def _wav_seconds(data: bytes) -> float:
    """`data` chunk size over the `fmt ` chunk's byte rate."""
    pos = 12
    byte_rate: int | None = None
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt " and size >= 16:
            byte_rate = int.from_bytes(data[body + 8 : body + 12], "little")
        elif chunk_id == b"data":
            if not byte_rate:
                break
            # a streamed WAV writes a placeholder size; what is there counts
            return min(size, len(data) - body) / byte_rate
        pos = body + size + (size & 1)  # chunks are padded to even sizes
    raise AudioError("the WAV file has no readable format or data chunk")


# MPEG audio Layer III frame headers: kbit/s by bitrate index and Hz by
# sample-rate index, MPEG-1 and MPEG-2/2.5.
_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data: bytes, pos: int) -> tuple[int, float] | None:
    """(frame length in bytes, frame duration in seconds) of a Layer III
    frame header at `pos`, or `None` when there is none."""
    if pos + 4 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    version = (data[pos + 1] >> 3) & 0x3  # 3 MPEG-1, 2 MPEG-2, 0 MPEG-2.5
    layer = (data[pos + 1] >> 1) & 0x3  # 1 Layer III
    bitrate_index = data[pos + 2] >> 4
    rate_index = (data[pos + 2] >> 2) & 0x3
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    padding = (data[pos + 2] >> 1) & 0x1
    sample_rate = _SAMPLE_RATES[version][rate_index]
    if version == 3:
        kbps, samples, factor = _BITRATES_V1[bitrate_index], 1152, 144_000
    else:
        kbps, samples, factor = _BITRATES_V2[bitrate_index], 576, 72_000
    return factor * kbps // sample_rate + padding, samples / sample_rate


def _mp3_seconds(data: bytes) -> float:
    """The sum of every frame's duration, from the first frame that is
    followed by another (or ends the file) to the first non-frame."""
    start = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = 0
        for byte in data[6:10]:  # syncsafe: seven bits per byte
            size = (size << 7) | (byte & 0x7F)
        start = 10 + size + (10 if data[5] & 0x10 else 0)
    pos = None
    for candidate in range(start, min(len(data), start + _MP3_SYNC_WINDOW)):
        frame = _mp3_frame(data, candidate)
        if frame is None:
            continue
        following = candidate + frame[0]
        # one valid header can be chance; two in a row are a stream
        if following >= len(data) or _mp3_frame(data, following) is not None:
            pos = candidate
            break
    if pos is None:
        raise AudioError("this is neither a WAV nor an MP3 file; only those can be transcribed")
    seconds = 0.0
    while (frame := _mp3_frame(data, pos)) is not None:
        length, duration = frame
        seconds += duration
        pos += length
    return seconds
