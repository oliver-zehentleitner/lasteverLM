# Providers

## Embeddings ask for their width; completions run with reasoning off

**Id:** b9d85733-8d1c-4e09-ac53-cb9303f871b0
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** probe of the inference platform, 2026-10-12 (models list, embeddings with and without `dimensions`, six completion variants); infra's first real run reported by agent A
**Revisit when:** the embedding or language model changes, or the provider stops honouring `dimensions` or `chat_template_kwargs`

The embedder sends `dimensions=EMBEDDING_DIMENSION` on every request and
refuses a vector of another width. The language model provider sends
`chat_template_kwargs: {"enable_thinking": false}` unless the instance or
the call asks for `thinking=True`; with thinking off, an empty answer is
an error. `max_tokens` defaults to `LLM_MAX_TOKENS` (1024).

**Reason:** two facts of the platform, both measured. The only embedding
model returns 2560 dimensions natively, above the 2000 that pgvector can
index; the API honours the OpenAI `dimensions` parameter and returned
1024 when asked. The language model is a reasoning model: with
`max_tokens=40` the content came back empty, `finish_reason` was
`length` and 39 of 40 completion tokens were reasoning; with a budget of
400 the answer arrived after the thinking, prefixed by a blank line. The
family's prompts (source summaries, cited answers with `[n]` markers,
output generators) are extraction over given text and gain nothing from
hidden reasoning, while every reasoning token is billed. Off is the
default; a generator that wants it can ask.

**Rejected alternative:** a large default `max_tokens` and thinking left
on. Rejected because the thinking is paid for on every call and the
answer still depends on the budget not running out mid-thought.

**Rejected alternative:** `reasoning_effort: "none"` or
`reasoning: {"enabled": false}` as the switch. Both worked on the platform
too; `chat_template_kwargs.enable_thinking` is the one the model
family's own serving stack documents, so it is the one sent. A top-level
`enable_thinking` was ignored. The switch is one function
(`thinking_body`), so changing it is one line.

**Rejected alternative:** let callers pass raw `extra_body`. Rejected
because the provider's quirks then leak into three services; the
interface stays `complete(system, user, max_tokens, thinking=)`.

## An unusable embedding vector fails the call

**Id:** 3310d44f-d79f-4da6-abdc-ad94e857529c
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** rag's request after a ready PDF whose chunks never came back in retrieval, 2026-10-13 (cause not proven); pgvector's cosine distance against a zero or non-finite vector is NaN
**Revisit when:** the platform is shown to return such vectors for a legitimate input class that should be indexed anyway

`OpenAICompatibleEmbedder.embed` raises `ProviderError` when a vector
has a NaN or infinite component or a zero norm, naming the input index.
The check runs after the count and width checks, in pure Python.

**Reason:** such a vector is stored without complaint but can never be
found. Its cosine distance is NaN, the similarity floor drops it, and the
source stays `ready` while nothing in it is searchable. An error makes
ingest fail that source visibly and turns a bad query or output embedding
in rag into a 502 or a retry. Any of those beats a silent gap.

**Rejected alternative:** skip the bad vector and store the rest. The
source would look complete with a hole in it, the same silence on a
smaller scale.

**Rejected alternative:** a check in SQL or in each service. Every
service would need it; the embedder is the one place all vectors pass.

## Speech speed is passed through, not range-checked in core

**Id:** dde2dae8-3797-4896-95fc-f457b45593d8
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** live probe of the platform's speech endpoint by rag, 2026-10-13 (0.25 to 4 accepted, anything else a 400 "speed must be between 0.25 and 4"; 0.5 roughly doubles the audio, 2.0 halves it)
**Revisit when:** a second speech provider with another range is added, or the platform's 400 stops naming the problem

`synthesize(text, voice=, speed=)` sends `speed` only when a caller gives
one and does not check its range. The platform's 400 arrives as a
`ProviderError`.

**Reason:** the range belongs to the provider and is enforced there, with
a clear message. A copy in core would be one more number to keep in step.
Leaving `speed` out when it is not given keeps the request of every
existing caller byte-identical.

**Rejected alternative:** clamp or validate 0.25 to 4 in core. Clamping
would hide a caller's mistake; validating duplicates the platform's own
check.

## A response without usage is an error, and a billed failure carries its usage

**Id:** 4f4b0bfc-d12e-4f25-a3ce-4e96e1810132
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the coordinator's decision on the core audit, 2026-10-14 (fail loud, per the family rule); rag's audit finding that an empty answer was billed but never recorded
**Revisit when:** a provider is added that legitimately reports no usage, or billing moves to the provider's own invoice data

When the embeddings or completions response has no `usage`, the call is a
`ProviderError`. It used to be accounted at zero tokens. `ProviderError`
has a `usage` field: when the provider answered and billed the call but
the answer is unusable (empty with thinking off, a bad vector, a wrong
count), the error carries that usage. The caller records it and then
lets the error propagate.

**Reason:** a missing usage block turned a paid call into a free one
without any trace, which is the silent failure the family rule forbids.
And an empty answer was raised before anyone could see what it had cost,
so the platform billed tokens that never reached the ledger. Carrying
the usage on the error keeps one exception type and leaves recording to
the caller, who owns the transaction.

**Rejected alternative:** zero tokens and a warning. The balance would
still be wrong, only in the logs.

**Rejected alternative:** record the usage inside the provider. Core's
providers know no connection, and the caller's transaction would no
longer decide what gets written.

## Transcription is billed per second, as the platform reports it

**Id:** 1da06e7e-a292-4729-9a86-178d26584373
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the coordinator's go for audio sources, 2026-10-14 (per-second billing, fail loud without usage); the platform's model catalog (whisper-large-v3-turbo priced per second); a live probe of `/audio/transcriptions` the same day
**Revisit when:** the platform stops reporting `usage.type = "duration"`, prices transcription per file or per token, or accepts formats beyond WAV and MP3

An `stt` usage row holds whole seconds of audio in `tokens_in`, rounded
up per call, and `MODEL_PRICES` gives `in` in cents per million seconds,
the same shape as `tts` with characters. The seconds are the ones the
platform reports in `usage: {"type": "duration", "seconds": n}`. The
duration core measures from the file (`probe_audio`) checks the four-hour
limit before the call, and it is the usage a `ProviderError` carries
when a response arrives without a duration. So the call fails loud and
is still recorded.

**Reason:** the platform prices the model per second, so per second is
the unit that matches its invoice. Its reported number is what it bills:
the probe returned 5 seconds for a WAV that measures 4.4 s and for an
MP3 that measures 5.04 s (encoder padding), so the platform's own count
is what the ledger should hold, not core's estimate of it. Core still has
to measure the file, because the four-hour limit has to be checked before
the call. Then the measurement can also stand in for the billing
evidence when the reported duration is missing. Measuring is plain
Python (WAV chunks, every MP3 frame header) because the common audio
library is GPL-licensed and core is MIT.

**Rejected alternative:** a flat price per file. A one-minute memo and a
four-hour lecture would cost the same, while the platform bills them
240 times apart.

**Rejected alternative:** bill the measured duration always. It disagrees
with the platform by up to a second per call, in both directions.

**Rejected alternative:** a silent fallback to the measured duration
when usage is missing. That is the free-call-without-trace case the rule
"a response without usage is an error" exists for. The error carries the
measured usage instead, so the caller records it and the source fails.

Multipart upload is used although the catalog describes a base64 JSON
body (`input_audio`): the probe accepted both, and multipart is what the
SDK sends without a hand-built request.

## A lasting 4xx is a permanent ProviderError

**Id:** 432dac6f-122a-48ff-b263-4ade23848ff8
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** ingest's requirement, relayed by the coordinator, 2026-10-14: a refused audio file must fail its source once, not three times at full price
**Revisit when:** a provider answers a lasting refusal with a status outside 4xx, or one of 408, 409 or 429 turns out to be final on the platform

`ProviderError.permanent` is `True` when the request was refused with a
4xx other than 408, 409 or 429, for every provider call, not only for
transcription. A worker that retries failed jobs fails such a job at
once.

**Reason:** a file over the platform's limits, an unknown voice or a
malformed request gets the same answer on every attempt, and with audio
each attempt may upload and bill up to four hours. 408, 409 and 429 say
"not now" rather than "never", and 5xx, timeouts and connection errors
are the cases retries exist for, so they stay retryable.

**Rejected alternative:** a separate exception class. Every caller
already catches `ProviderError`, and a subclass that some caller forgot
would be retried or, worse, escape the worker. A flag needs nothing from
callers that ignore it.

## Speech synthesis is billed per second of generated speech, from the response header

**Id:** b57ab97c-0eee-43b3-a7ee-06226e2d9714
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** rag's measurement of `/audio/speech` and the platform's model catalog ("completion", unit "second"), relayed by the coordinator, 2026-10-14; a live probe the same day (6 characters: `x-usage-seconds: 1`, 0.89 s of MP3; 97 characters: 7, 6.79 s)
**Revisit when:** the platform reports speech usage in a response body, or prices it per character or per token

A `tts` usage row holds the seconds of generated speech in `tokens_in`,
read from the `x-usage-seconds` response header and rounded up, and
`MODEL_PRICES` gives `in` in cents per million seconds, the same unit as
`stt` (the entry above). `synthesize` returns the audio and that usage
together (`SpeechResult`). A response without a readable header is a
`ProviderError` carrying the duration measured from the MP3, so the call
fails loud and is still recorded. This replaces the per-character unit
that the family's audio-output decision set
(`context/architecture.md`, "Audio files are served through presigned
URLs").

**Reason:** the platform bills speech by its length, not its input. At
the configured price, counting characters charged users about fifteen
times what the platform charged. The header is the only place the
platform states the billed seconds (there is no usage body), and like
the transcription duration it is the number on the invoice. Returning
the usage from `synthesize` puts the unit in core, next to the call,
instead of leaving every caller to pick one.

**Rejected alternative:** keep characters and lower the price to an
average seconds-per-character rate. Speech rate depends on voice, speed
and language. A voice preview at speed 0.5 would be billed at half its
cost.

**Rejected alternative:** bill the measured MP3 duration. It is the
fallback only. It is a fraction of a second shorter than what the
platform reports (0.89 s against 1), so the ledger would undercount
every call.
