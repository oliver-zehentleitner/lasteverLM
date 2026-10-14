"""The providers through the real OpenAI SDK against a fake HTTP transport, so
what the server receives (the `dimensions` parameter, the thinking switch)
is what is asserted, not what a stub was handed. httpx2 is the HTTP client
the OpenAI SDK (3.x) is built on."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from openai import OpenAI

from lasteverlm_core import (
    DEFAULT_TTS_VOICE,
    AudioError,
    InferenceSettings,
    OpenAICompatibleEmbedder,
    OpenAICompatibleLLM,
    OpenAICompatibleSpeechToText,
    OpenAICompatibleTextToSpeech,
    ProviderError,
    SpeechResult,
    Usage,
    make_client,
    thinking_body,
)

# MPEG-1 Layer III, 128 kbit/s, 44.1 kHz: 417 bytes and 1152 samples a frame,
# so 200 frames are 5.22 s.
MP3 = b"\xff\xfb\x90\x00" + bytes(413)


class FakeServer:
    """Records every request body and answers from a queue of responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        # json.dumps writes NaN and Infinity as the SDK's parser accepts them;
        # httpx's json= refuses them, and a provider may still send them.
        return httpx.Response(
            200,
            content=json.dumps(self.responses.pop(0)).encode(),
            headers={"content-type": "application/json"},
        )

    def client(self) -> OpenAI:
        return OpenAI(
            base_url="http://fake/v1",
            api_key="test",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            max_retries=0,
        )


def embeddings(*vectors: list[float], tokens: int = 9) -> dict[str, Any]:
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    return {
        "object": "list",
        "data": list(reversed(data)),
        "model": "embed",
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


def completion(
    content: str | None, finish: str = "stop", reasoning_tokens: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "llm",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, **extra},
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 5 + reasoning_tokens,
            "total_tokens": 16 + reasoning_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def test_embedder_requests_the_configured_dimension() -> None:
    server = FakeServer()
    server.responses.append(embeddings([0.5] * 4, [1.0] * 4))
    result = OpenAICompatibleEmbedder(server.client(), "embed", 4).embed(["a", "b"])
    assert server.requests[0]["dimensions"] == 4
    assert server.requests[0]["input"] == ["a", "b"]
    assert result.vectors == [[0.5] * 4, [1.0] * 4]  # ordered by index, not arrival
    assert (result.usage.kind, result.usage.model, result.usage.tokens_in) == (
        "embedding",
        "embed",
        9,
    )


def test_embedder_rejects_a_vector_of_another_width() -> None:
    server = FakeServer()
    server.responses.append(embeddings([0.0] * 2560))
    with pytest.raises(ProviderError, match="2560 dimensions although 1024 were requested"):
        OpenAICompatibleEmbedder(server.client(), "embed", 1024).embed(["a"])


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ([0.0, 0.0, 0.0, 0.0], "zero norm"),
        ([1.0, float("nan"), 0.0, 0.0], "non-finite"),
        ([float("inf"), 0.0, 0.0, 0.0], "non-finite"),
    ],
)
def test_embedder_rejects_an_unusable_vector(bad: list[float], reason: str) -> None:
    server = FakeServer()
    server.responses.append(embeddings([1.0, 0.0, 0.0, 0.0], bad))
    with pytest.raises(ProviderError, match=rf"unusable vector \({reason}.*for input 1"):
        OpenAICompatibleEmbedder(server.client(), "embed", 4).embed(["a", "b"])


def test_embedder_empty_input_makes_no_request() -> None:
    server = FakeServer()
    result = OpenAICompatibleEmbedder(server.client(), "embed", 4).embed([])
    assert result.vectors == [] and server.requests == []


def test_llm_disables_thinking_by_default_and_reports_tokens() -> None:
    server = FakeServer()
    server.responses.append(completion("\n\nVienna.", reasoning=None))
    llm = OpenAICompatibleLLM(server.client(), "llm")
    result = llm.complete("sys", "usr")
    body = server.requests[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 1024
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["temperature"] == 0.0
    assert result.text == "Vienna."
    assert (result.usage.tokens_in, result.usage.tokens_out, result.finish_reason) == (
        11,
        5,
        "stop",
    )


def test_llm_thinking_per_call_and_per_instance() -> None:
    server = FakeServer()
    server.responses += [completion("a"), completion("b"), completion("c")]
    llm = OpenAICompatibleLLM(server.client(), "llm", max_tokens=300, thinking=True)
    llm.complete("s", "u")
    llm.complete("s", "u", thinking=False)
    llm.complete("s", "u", 40)
    assert [r["chat_template_kwargs"]["enable_thinking"] for r in server.requests] == [
        True,
        False,
        True,
    ]
    assert [r["max_tokens"] for r in server.requests] == [300, 300, 40]
    assert thinking_body(False) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_llm_empty_answer_without_thinking_is_an_error() -> None:
    server = FakeServer()
    server.responses.append(completion("", finish="length"))
    with pytest.raises(ProviderError, match="empty answer.*length") as caught:
        OpenAICompatibleLLM(server.client(), "llm").complete("s", "u", 40)
    # billed all the same: the caller records this before re-raising
    usage = caught.value.usage
    assert usage is not None and (usage.kind, usage.tokens_in, usage.tokens_out) == ("llm", 11, 5)


def test_a_response_without_usage_is_an_error_not_a_free_call() -> None:
    server = FakeServer()
    no_usage = completion("Vienna.")
    del no_usage["usage"]
    server.responses.append(no_usage)
    with pytest.raises(ProviderError, match="language model llm reported no usage") as caught:
        OpenAICompatibleLLM(server.client(), "llm").complete("s", "u")
    assert caught.value.usage is None
    vectors = embeddings([1.0, 0.0, 0.0, 0.0])
    del vectors["usage"]
    server.responses.append(vectors)
    with pytest.raises(ProviderError, match="embedding model embed reported no usage"):
        OpenAICompatibleEmbedder(server.client(), "embed", 4).embed(["a"])


def test_a_rejected_vector_carries_the_billed_usage() -> None:
    server = FakeServer()
    server.responses.append(embeddings([0.0, 0.0, 0.0, 0.0], tokens=7))
    with pytest.raises(ProviderError, match="zero norm") as caught:
        OpenAICompatibleEmbedder(server.client(), "embed", 4).embed(["a"])
    assert caught.value.usage == Usage("embedding", "embed", 7, 0)


def test_make_client_takes_the_timeout_from_the_settings() -> None:
    env = {"INFERENCE_API_KEY": "k", "LLM_MODEL": "l", "EMBEDDING_MODEL": "e"}
    settings = InferenceSettings.from_env({**env, "LLM_TIMEOUT_SECONDS": "180"})
    assert make_client(settings).timeout == 180
    assert make_client(settings, timeout=5).timeout == 5
    assert make_client(InferenceSettings.from_env(env)).timeout == 60


def test_llm_empty_answer_with_thinking_is_returned_for_the_caller_to_judge() -> None:
    server = FakeServer()
    server.responses.append(
        completion("", finish="length", reasoning_tokens=39, reasoning="We need")
    )
    result = OpenAICompatibleLLM(server.client(), "llm", thinking=True).complete("s", "u", 40)
    assert result.text == "" and result.finish_reason == "length"
    assert result.usage.tokens_out == 44  # reasoning tokens are billed completion tokens


def test_llm_http_error_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad dimensions"}})

    client = OpenAI(
        base_url="http://fake/v1",
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    with pytest.raises(ProviderError, match="completion request failed"):
        OpenAICompatibleLLM(client, "llm").complete("s", "u")
    with pytest.raises(ProviderError, match="embedding request failed"):
        OpenAICompatibleEmbedder(client, "embed", 4).embed(["a"])


def speech_client(
    requests: list[dict[str, Any]],
    status: int = 200,
    content: bytes = b"\xff\xf3mp3",
    seconds: str | None = "2",
) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Unknown voice"}})
        headers = {"content-type": "audio/mpeg"}
        if seconds is not None:
            headers["x-usage-seconds"] = seconds
        return httpx.Response(200, content=content, headers=headers)

    return OpenAI(
        base_url="http://fake/v1",
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def test_tts_sends_the_platform_default_voice() -> None:
    requests: list[dict[str, Any]] = []
    tts = OpenAICompatibleTextToSpeech(speech_client(requests), "tts")
    assert tts.synthesize("Hallo.") == SpeechResult(b"\xff\xf3mp3", Usage("tts", "tts", 2, 0))
    assert DEFAULT_TTS_VOICE == "ryan" and tts.voice == "ryan"
    assert requests == [{"model": "tts", "voice": "ryan", "input": "Hallo."}]


def test_tts_voice_and_speed_per_call() -> None:
    requests: list[dict[str, Any]] = []
    tts = OpenAICompatibleTextToSpeech(speech_client(requests), "tts", voice="serena")
    tts.synthesize("a", voice="eric", speed=1.5)
    tts.synthesize("b", speed=0.5)
    tts.synthesize("c")
    assert requests == [
        {"model": "tts", "voice": "eric", "input": "a", "speed": 1.5},
        {"model": "tts", "voice": "serena", "input": "b", "speed": 0.5},
        {"model": "tts", "voice": "serena", "input": "c"},
    ]


def test_tts_bills_the_reported_seconds_rounded_up() -> None:
    tts = OpenAICompatibleTextToSpeech(speech_client([], seconds="6.2"), "tts")
    assert tts.synthesize("Hallo.").usage == Usage("tts", "tts", 7, 0)


@pytest.mark.parametrize("seconds", [None, "", "seven", "-1", "nan"])
def test_tts_without_reported_seconds_fails_with_the_measured_ones(seconds: str | None) -> None:
    # 100 MPEG-1 frames at 44.1 kHz: 2.61 s, billed as 3
    tts = OpenAICompatibleTextToSpeech(speech_client([], content=MP3 * 100, seconds=seconds), "tts")
    with pytest.raises(ProviderError, match="reported no x-usage-seconds") as caught:
        tts.synthesize("Hallo.")
    assert caught.value.usage == Usage("tts", "tts", 3, 0)


def test_tts_unmeasurable_audio_without_seconds_fails_without_usage() -> None:
    tts = OpenAICompatibleTextToSpeech(speech_client([], content=b"noise", seconds=None), "tts")
    with pytest.raises(ProviderError, match="reported no x-usage-seconds") as caught:
        tts.synthesize("Hallo.")
    assert caught.value.usage is None


def test_tts_empty_audio_is_an_error_with_its_usage() -> None:
    tts = OpenAICompatibleTextToSpeech(speech_client([], content=b"", seconds="1"), "tts")
    with pytest.raises(ProviderError, match="returned no audio") as caught:
        tts.synthesize("Hallo.")
    assert caught.value.usage == Usage("tts", "tts", 1, 0)


def test_tts_http_error_becomes_provider_error() -> None:
    tts = OpenAICompatibleTextToSpeech(speech_client([], status=400), "tts", voice="alloy")
    with pytest.raises(ProviderError, match="speech request failed") as caught:
        tts.synthesize("Hallo.")
    assert caught.value.permanent is True  # an unknown voice stays unknown


def transcription_client(
    requests: list[httpx.Request], answer: dict[str, Any] | None, status: int = 200
) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad audio"}})
        return httpx.Response(200, json=answer)

    return OpenAI(
        base_url="http://fake/v1",
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def test_stt_sends_the_file_as_multipart_and_bills_the_reported_seconds() -> None:
    requests: list[httpx.Request] = []
    answer = {"text": " Hallo Welt. ", "usage": {"type": "duration", "seconds": 5.2}}
    stt = OpenAICompatibleSpeechToText(transcription_client(requests, answer), "whisper")
    result = stt.transcribe(MP3 * 200, "talk.mp3")
    assert result.text == "Hallo Welt."
    assert result.usage == Usage("stt", "whisper", 6, 0)  # rounded up
    assert result.seconds == pytest.approx(200 * 1152 / 44100)
    (request,) = requests
    assert request.url.path == "/v1/audio/transcriptions"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="model"\r\n\r\nwhisper' in body
    assert b'name="response_format"\r\n\r\njson' in body
    assert b'filename="talk.mp3"' in body and b"Content-Type: audio/mpeg" in body


def test_stt_without_reported_usage_fails_with_the_measured_seconds() -> None:
    stt = OpenAICompatibleSpeechToText(transcription_client([], {"text": "Hallo"}), "whisper")
    with pytest.raises(ProviderError, match="reported no duration usage") as caught:
        stt.transcribe(MP3 * 200, "talk.mp3")
    assert caught.value.usage == Usage("stt", "whisper", 6, 0)


def test_stt_empty_transcript_is_an_error_with_its_usage() -> None:
    answer = {"text": "  ", "usage": {"type": "duration", "seconds": 5}}
    stt = OpenAICompatibleSpeechToText(transcription_client([], answer), "whisper")
    with pytest.raises(ProviderError, match="recognised no speech") as caught:
        stt.transcribe(MP3 * 200, "talk.mp3")
    assert caught.value.usage == Usage("stt", "whisper", 5, 0)


def test_stt_refuses_unsupported_audio_before_the_call() -> None:
    requests: list[httpx.Request] = []
    stt = OpenAICompatibleSpeechToText(transcription_client(requests, None), "whisper")
    with pytest.raises(AudioError, match="Ogg"):
        stt.transcribe(b"OggS" + bytes(100), "talk.ogg")
    assert requests == []


@pytest.mark.parametrize(
    ("status", "permanent"),
    [(400, True), (413, True), (422, True), (408, False), (429, False), (500, False)],
)
def test_stt_http_error_is_permanent_only_for_a_lasting_refusal(
    status: int, permanent: bool
) -> None:
    stt = OpenAICompatibleSpeechToText(transcription_client([], None, status=status), "whisper")
    with pytest.raises(ProviderError, match="transcription request failed") as caught:
        stt.transcribe(MP3 * 10, "talk.mp3")
    assert caught.value.usage is None
    assert caught.value.permanent is permanent


def test_a_connection_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = OpenAI(
        base_url="http://fake/v1",
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    with pytest.raises(ProviderError, match="transcription request failed") as caught:
        OpenAICompatibleSpeechToText(client, "whisper").transcribe(MP3 * 10, "talk.mp3")
    assert caught.value.permanent is False
