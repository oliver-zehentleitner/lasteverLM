"""Provider interfaces, one implementation each over an OpenAI-compatible API."""

from .base import (
    LLM,
    CompletionResult,
    Embedder,
    EmbeddingResult,
    ProviderError,
    SpeechResult,
    SpeechToText,
    TextToSpeech,
    TranscriptionResult,
    Usage,
)
from .openai_compat import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TTS_VOICE,
    OpenAICompatibleEmbedder,
    OpenAICompatibleLLM,
    OpenAICompatibleSpeechToText,
    OpenAICompatibleTextToSpeech,
    make_client,
    thinking_body,
)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TTS_VOICE",
    "LLM",
    "CompletionResult",
    "Embedder",
    "EmbeddingResult",
    "OpenAICompatibleEmbedder",
    "OpenAICompatibleLLM",
    "OpenAICompatibleSpeechToText",
    "OpenAICompatibleTextToSpeech",
    "ProviderError",
    "SpeechResult",
    "SpeechToText",
    "TextToSpeech",
    "TranscriptionResult",
    "Usage",
    "make_client",
    "thinking_body",
]
