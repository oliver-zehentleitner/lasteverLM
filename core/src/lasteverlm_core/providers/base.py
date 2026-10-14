"""The provider interfaces every service codes against.

Each call reports its token usage so the caller can write the `usage`
row. A response without usage is a `ProviderError`, never a free call,
and an error raised after a response arrived carries that response's
usage, so the caller records what was billed before it re-raises. Another provider is one more
module implementing these protocols, selected by configuration; callers
do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import UsageKind


class ProviderError(Exception):
    """The provider request failed or returned something unusable.

    `usage` is set when the provider answered and billed the call (an
    empty answer, an unusable vector): the caller records it, then lets
    the error propagate. It is `None` when no usable response came back.

    `permanent` is set when the platform refused the request with a 4xx
    that a retry will not change: a worker fails the job at once instead
    of paying for more attempts. Everything else (5xx, timeouts, 408, 409,
    429, an unusable answer) is `False`, retryable as before.
    """

    def __init__(
        self, message: str, usage: Usage | None = None, *, permanent: bool = False
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.permanent = permanent


@dataclass(frozen=True)
class Usage:
    """Token counts of one call, ready for `record_usage`."""

    kind: UsageKind
    model: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class EmbeddingResult:
    """One vector per input, in input order, and the usage to record."""

    vectors: list[list[float]]
    usage: Usage


@dataclass(frozen=True)
class CompletionResult:
    """The answer text, the usage to record and why the model stopped."""

    text: str
    usage: Usage
    # "stop" when the model finished, "length" when max_tokens cut it off.
    finish_reason: str | None = None


@dataclass(frozen=True)
class SpeechResult:
    """The MP3 bytes and the usage to record (kind `tts`, seconds of
    generated speech in `tokens_in`)."""

    audio: bytes
    usage: Usage


@dataclass(frozen=True)
class TranscriptionResult:
    """The transcript, the usage to record (kind `stt`, seconds in
    `tokens_in`) and the duration core measured from the file."""

    text: str
    usage: Usage
    seconds: float


class Embedder(Protocol):
    """Turns texts into vectors of `dimension` floats; services are typed against this."""

    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> EmbeddingResult: ...


class LLM(Protocol):
    """One system plus one user message in, one completion out."""

    @property
    def model(self) -> str: ...

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        *,
        thinking: bool | None = None,
    ) -> CompletionResult: ...


class TextToSpeech(Protocol):
    """Text in, MP3 bytes and their usage (per second of speech) out."""

    @property
    def model(self) -> str: ...

    def synthesize(
        self, text: str, *, voice: str | None = None, speed: float | None = None
    ) -> SpeechResult: ...


class SpeechToText(Protocol):
    """WAV or MP3 bytes in, the transcript and its usage (per second) out."""

    @property
    def model(self) -> str: ...

    def transcribe(self, audio: bytes, filename: str) -> TranscriptionResult: ...
