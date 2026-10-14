"""Format detection, duration and the platform's limits, on files built here."""

from __future__ import annotations

import io
import wave

import pytest

from lasteverlm_core import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    AudioError,
    billed_seconds,
    measure_audio,
    probe_audio,
)

# MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417 bytes, 1152 samples.
MP3_FRAME = b"\xff\xfb\x90\x00" + bytes(413)
MP3_FRAME_SECONDS = 1152 / 44100
# MPEG-2.5 Layer III, 8 kbit/s, 8 kHz: 72 bytes, 576 samples (0.072 s).
SLOW_FRAME = b"\xff\xe3\x18\x00" + bytes(68)


def wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(2 * int(rate * seconds)))
    return buffer.getvalue()


def id3(size: int) -> bytes:
    syncsafe = bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x04\x00\x00" + syncsafe + bytes(size)


def test_wav_duration_from_its_chunks() -> None:
    info = probe_audio(wav(2.5))
    assert (info.format, info.mime, info.seconds) == ("wav", "audio/wav", 2.5)


def test_mp3_duration_is_the_sum_of_its_frames() -> None:
    info = probe_audio(MP3_FRAME * 100)
    assert (info.format, info.mime) == ("mp3", "audio/mpeg")
    assert info.seconds == pytest.approx(100 * MP3_FRAME_SECONDS)


def test_mp3_skips_the_id3_tag_and_stops_at_trailing_data() -> None:
    data = id3(300) + MP3_FRAME * 10 + b"TAG" + bytes(125)
    assert probe_audio(data).seconds == pytest.approx(10 * MP3_FRAME_SECONDS)


def test_a_lone_sync_pattern_is_not_a_stream() -> None:
    # a chance 0xFFFB before the real frames is skipped, not counted
    data = b"junk\xff\xfb\x90\x00junk" + MP3_FRAME * 3
    assert probe_audio(data).seconds == pytest.approx(3 * MP3_FRAME_SECONDS)


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (b"OggS" + bytes(100), "Ogg"),
        (b"fLaC" + bytes(100), "FLAC"),
        (b"\x00\x00\x00\x20ftypM4A " + bytes(100), "MP4/M4A"),
        (b"\x1aE\xdf\xa3" + bytes(100), "WebM"),
        (b"%PDF-1.7" + bytes(100), "neither a WAV nor an MP3"),
    ],
)
def test_other_formats_are_refused_by_name(data: bytes, named: str) -> None:
    with pytest.raises(AudioError, match=named):
        probe_audio(data)


def test_empty_and_silent_files_are_refused() -> None:
    with pytest.raises(AudioError, match="empty"):
        probe_audio(b"")
    with pytest.raises(AudioError, match="no audio"):
        probe_audio(wav(0))
    with pytest.raises(AudioError, match="no readable"):
        probe_audio(b"RIFF\x00\x00\x00\x00WAVE")


def test_the_platform_limits_are_checked_before_any_call() -> None:
    with pytest.raises(AudioError, match="25 MiB"):
        probe_audio(MP3_FRAME * (MAX_AUDIO_BYTES // len(MP3_FRAME) + 1))
    # 8 kbit/s fits more than four hours into 25 MiB
    frames = int(MAX_AUDIO_SECONDS / 0.072) + 2
    assert len(SLOW_FRAME) * frames < MAX_AUDIO_BYTES
    with pytest.raises(AudioError, match="4 hours"):
        probe_audio(SLOW_FRAME * frames)


def test_measure_audio_skips_the_transcription_limits() -> None:
    long_speech = SLOW_FRAME * (int(MAX_AUDIO_SECONDS / 0.072) + 2)
    assert measure_audio(long_speech).seconds > MAX_AUDIO_SECONDS
    with pytest.raises(AudioError, match="empty"):
        measure_audio(b"")


def test_billed_seconds_round_up_to_at_least_one() -> None:
    assert billed_seconds(4.4) == 5
    assert billed_seconds(5.0) == 5
    assert billed_seconds(0.01) == 1
