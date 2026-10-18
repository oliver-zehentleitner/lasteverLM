# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Clear German audio requests that the planner misses now produce a priced interview plan; a missing or unreadable source gets a readable prompt before planning, and internal ingest validation details stay out of user-facing errors.

## [0.22.22] - 2026-10-16

### Fixed

- A chat question in a notebook that holds a chart (or a decision or flashcards) is answered again: the document list had no label for those kinds and every question failed with 500. Every output and source kind has a label now, an unknown kind is shown by its name with a warning instead of failing, and an uploaded audio file is listed as "audio file", no longer as "audio overview".

## [0.22.21] - 2026-10-16

### Added

- `lasteverlm-rag-repair-copies`: restores a cited passage that a starter copy lost and maps the starter's source ids left in a copy's table or infographic options and coverage; mapped by source name and chunk position, a dry run by default, idempotent.

### Fixed

- An output's judge count is per unit: `provenance.judge.final` (and `final` on a judgement on request and at standard) counts each judged unit once by its last verdict, `supported_elsewhere` as passed. Before, the head showed the last round's count, so eight paragraphs that all ended supported read "1 of 8 passed".
- A starter copy keeps every cited passage: one whose chunk is not in the copy (its source was unlinked from the starter) stays with its quote and no chunk, where it was dropped and its citation opened nothing. A copied table's or infographic's picked candidates and the coverage get the copy's source ids instead of the starter's.

## [0.22.20] - 2026-10-16

### Fixed

- `mp3.py` is analysed by CodeQL again: its first docstring line read "decoding: duration", which the extractor took for a PEP 263 encoding declaration (`duration`) and skipped the file. Reworded; a test keeps `coding:`/`coding=` out of the first two lines of every rag source file.

## [0.22.19] - 2026-10-15

### Changed

- An audio's voices follow its output language when the user sent none, as the maintainer chose them by listening: German Dylan and Ono Anna (second voice), English Ryan and Vivian; other languages keep the default. The options catalogue gives `audio.defaults_by_language` and `audio.recommended` (voice ids per language: German Dylan, Eric, Uncle Fu, Ono Anna, Sohee; English Ryan, Aiden, Vivian, Serena).

## [0.22.18] - 2026-10-15

### Added

- `lasteverlm-rag-repair-reports`: re-splits stored reports into their checkable units from the stored markdown and runs the word check again, no model call; a dry run by default, listing each affected output with its unit counts; idempotent.
- A claim the judge finds `not_in_source` is searched for again across the output's selected sources before anything is revised, and judged once more with the passages it did not cite. Supported now, its verdict is `supported_elsewhere`: it passes, its `refs` gain the passages found (`judge.added_refs`), and a passage new to the output joins `provenance.passages` with `added_by: "judge"`; the word check runs again on it. `not_in_source` now means in no selected source; a `contradicted` claim is never searched again, so a conflict between sources stays visible. At generation (standard, thorough, bulletproof) and on request; rounds and judgements count `supported_elsewhere` and `searched_again`. The estimate counts the extra judge calls in its maximum.

### Changed

- The writer prompts ask for a citation for every detail (a name, a number, a step, a limit) and to leave out a detail no passage states, never fill it in; every generator's prompt version is one up.
- A job taken over by another worker (a handover at a deploy, a retry, a stale claim) resumes instead of starting over: every model call it finished (the draft, each judge verdict and revision, the decision model, the focus embeddings) is kept in the output's journal (`outputs.checkpoint`, migration `0019_rag_output_checkpoint.sql`) and answered from there, without calling or booking it again. The journal is capped, never logged, and cleared when the output is ready or failed.

### Fixed

- A report paragraph that follows its heading without a blank line is a checkable unit again: a heading is its own line only. Before, such paragraphs were dropped from `content.units`, so a report could be stored as ready with nothing checked (and a judge on request judged nothing). A text that cites passages but yields no checkable unit is now tried once more and then fails with a clear error; fewer units than a third of its citation markers is logged as a warning.
- A deploy no longer leaves a running output looking hung for ten minutes. A stopping worker claims nothing new and lets its job finish while its grace period (`WORKER_GRACE_SECONDS`) allows; with less than one model request's worst case left, it hands the job over at the next step: claim cleared (only while it still holds it), phase "handing over", the attempt not counted, an `attempt_log` entry `handover`. Another worker takes it within seconds.

## [0.22.16] - 2026-10-15

### Added

- `lasteverlm-rag-orphans`: counts, per notebook id, the generated sources no output points at that are older than an hour (read only, no names). `ORPHAN_SWEEP=true` lets the worker delete them once an hour; it is off by default.

### Fixed

- A job runs to one result. Its claim is refreshed by a thread every minute for as long as it runs, so a single long model request (180 s with up to two retries) or the indexing can no longer let another worker take over a live job. The ready write goes through only while the worker still holds the claim; a worker that lost it discards its own generated source and file. Before, both runs indexed, the later write won, and the other's generated source stayed in the notebook's sources with no output. The background refresh stops after `JOB_MAX_SECONDS` (default 45 min), logged as an error, so a job hung in a live process still goes stale and is taken over.

## [0.22.12] - 2026-10-15

### Fixed

- The agent chat makes a document from the sources the user means. Sources named in the message ("erstelle aus Senior_Database_Engineer.pdf eine Mindmap") are matched against the notebook's (exact name, then without case and extension, then a unique part); the plan uses only those, its "Reads" lists only them, and a name that matches nothing or several sources is a struck step with its reason. `create_output` takes `sources`, and the step `args.source_ids`. Without named sources the step uses the request's selection; when more than one is selected and the last answer cited only some, the plan asks "All N selected sources" or "Only <those> (just discussed)", the latter preselected. Before, every plan read all of the notebook's sources.
- A correction such as "falsches PDF", "nicht das", "nochmal mit …" or "redo with …" proposes the last plan's documents again with the source question, instead of being answered from the sources; without a plan to redo the chat asks which document to use.

## [0.22.9] - 2026-10-15

### Changed

- A judgement on request runs as a background job: `POST …/outputs/{id}/judge` and `…/messages/{id}/judge` answer 202 with a `JudgeJob` (status, phase, attempts, attempt log) instead of the judged output or message; poll `GET /api/rag/judge-jobs/{job_id}` (or the target's `GET …/judge`) and read the target when done. Migration `0018_rag_judge_jobs.sql`. A High check of many units no longer runs past the ingress' 120 s.

## [0.22.8] - 2026-10-15

### Added

- Find tables / Find themes: `POST …/tables/propose` and `…/infographics/propose` propose up to 6 candidates (previews with their sources) from the selected sources; a table or infographic made from picked candidates (`options.tables` / `options.themes`) gets one table or part per candidate, each from its own sources only. A candidate outside the selection is 409 `candidate_out_of_scope`. New usage actions `table_propose` and `infographic_propose`.
- Every output's provenance has `coverage`: which selected sources it cites, read from its units' refs, and for an uncited source the generator's own reason when it named it. Generators are asked to use every source that holds something fitting and to name one that holds nothing, and why, instead of covering every source at any cost; every generator's prompt version goes up by one.

### Changed

- The judge may use up to 6000 output tokens (was 4000; glm-5.3-flash used up to 3507 on long passages). A judge answer without a usable verdict and reason is recorded by its structure only (keys, the reason's type and length, finish reason, output tokens; never its text) as `answer_shape` on the unit and in the log.

## [0.22.5] - 2026-10-15

### Fixed

- A decision counts an evidence item as supported by its judge's verdict when it was judged, the word check only otherwise; disputed items are not counted and are named in `agreement_note`. Before, the count used the word check alone, so a decision in another language than its source showed "no decision" although its judges supported the evidence; agreement, weight and the shown number follow the corrected count.

## [0.22.4] - 2026-10-15

### Fixed

- A chart's number check no longer reads the positions of a structured source as values: a CSV's "row 3: ", an index "[0]" or an attribute "@href" (ingest's rendering contract) could make a point equal to a row number or index count as supported. E-notation ("1e-05") is now read as its value.

## [0.22.1] - 2026-10-15

### Added

- Verification level `bulletproof`: the high judge with revision and a second judge of another family (`SECOND_JUDGE_MODEL`, default `deepseek-v4.1-flash`, chosen by evaluation, `docs/second-judge-eval.md`) on every unit; supported only if both agree, removed if both fail, otherwise `disputed`, kept and marked. In outputs, chat answers (annotate only), the estimate's `levels` and the agent's question. An older `judge_default` of `high` now reads as bulletproof.

### Fixed

- A bulletproof unit keeps each judge's own verdict: `judge.first` (the high judge) and `judge.second`, each with model, verdict, reason and quote, also when the two agree. A disputed unit had lost the first judge's verdict and reason.

## [0.22.0] - 2026-10-15

### Added

- Verification levels. `options.verification`: `basic` (word and negation check, free, the default), `standard` (also a low judge, once, for each unit the check doubts, annotate only) or `thorough` (the medium judge with revision, as `judge: medium`). `POST /estimate` returns `levels` with each level's price. Chat answers follow the user's `verification_level` (else their `judge_default`), annotate only. The agent asks for the level instead of the judge, preselecting the user's.
- A negation check after the word check (method `lexical-overlap-v2`): a unit whose words its passages support, but which states the same predicate of the same subject with the opposite polarity ("is not open" over "is open"), is marked `possible_contradiction`, in outputs (not quizzes and flashcards) and chat answers. spaCy 3.8.7 with `en_core_web_sm` and `de_core_news_sm` 3.8.0 (pinned by hash), loaded once per process on first use: about +270 MB memory per process that checks, +446 MB image. Calibration in `docs/negation-eval.md`.

### Changed

- A decision says how much evidence stands behind its number: `supported_evidence` (supported items on both sides) and `weight`, `tendency` with one supported item or an unclear balance, `full` otherwise (a conflict included), `null` when undecided. The number is never hidden for it.

### Fixed

- Charts stay inside the selected sources. Finding charts takes `source_ids` and `url_refresh`, reads unread selected sources first (as a chat answer does) and searches only the selection; before, it proposed from the whole notebook. Creating a chart refuses a candidate citing a passage outside the selection, from a generated source or outside the notebook with 409 `chart_out_of_scope`; before, candidates from an earlier selection were drawn.

## [0.21.5] - 2026-10-15

### Fixed

- An undecided decision (no evidence item supported) carries no number: `model_answer` is `null` instead of the decision model's probability, which without evidence is its prior, not a decision from the sources. The number is kept in `provenance.model_answer_without_evidence`.
- An audio's segments are joined with one Xing header for the whole stream: each segment brought its own, so a player took the first segment's length for the file's (0:12, growing while playing) and could not seek past it ("play from here"). The later headers are dropped and the first is rewritten with the total frames, bytes, a new seek table and the LAME tag's length, padding and checksums; transcript times count audio frames only. `lasteverlm-rag-repair-audio` rewrites audio stored since 0.20.2 in place (a dry run unless `--apply`, idempotent) and moves its transcript times back accordingly.

## [0.21.4] - 2026-10-15

### Added

- Every failed attempt of an output is kept on the row as core's `AttemptRecord` (`outputs.attempt_log`, `Output.attempt_log`), and `outputs.finished_at` records when it became ready or failed (migration `0017_rag_output_attempts.sql`). `GET /api/rag/admin/outputs?id_prefix=` finds outputs by the start of their id for viewers and admins, with the owner (masked for viewers) and the attempts. Log lines name an output by its short id.

## [0.21.2] - 2026-10-15

### Changed

- Copying a starter notebook's outputs logs how many were copied, skipped as not ready and already copied. A starter notebook without ready outputs is logged at WARNING.

## [0.21.1] - 2026-10-15

### Fixed

- The chat's document list passes a generated document's name (an output title, which the user can rename) through the same flattening as an uploaded file's name: one line, markers defused, at most 120 characters. A title with a newline could otherwise start a line of its own in the system prompt.

## [0.21.0] - 2026-10-15

### Changed

- An output's creation and a plan's confirm start only if the balance covers their estimated maximum (with the reading of unread sources and pages fetched again) plus what the user's started work still holds: waiting outputs (`outputs.reserved_cents`, migration `0016_rag_output_reserved.sql`), a running plan's waiting explanations and ingest's funded reads. Otherwise 402 with `code: "credit_insufficient"`, `needed_cents`, `held_cents` and `available_cents`.
- A failed model call the user waits for (chat answer, planning, starting questions, voice sample) is tried once more. The second failure is 502 with `code: "model_no_answer"` and a `cause` the web can translate (`timeout`, `rate_limited`, `provider_error`, `empty_answer`, `truncated_answer`, `unparseable_answer`, `no_audio`, `unknown`), instead of the English "the model did not answer; please try again".

### Fixed

- A confirmed agent plan runs all its steps. Credit is checked once, at confirm (402 leaves the plan proposed), no longer before each ingest step, output and explanation, where a plan could stop half done once a step had spent the balance.

## [0.20.5] - 2026-10-15

### Changed

- A report's `length` is an upper bound (at most 500 / 1200 / 2500 words), no longer a word target: the prompt asks for less when the material says less and forbids adding facts, roles, steps or reasons the material does not state. Prompt version `report-<style>-v4`.

## [0.20.3] - 2026-10-15

### Fixed

- The support check no longer counts every output with a chosen `language` as translated. A unit is `unverified` only when it is in another language than its cited passages, told per unit; an English output from English sources with too little overlap is `unsupported` again.

## [0.20.2] - 2026-10-15

### Added

- Audio transcript times for highlighting: each `content.transcript` entry gets `start` and `end`, its seconds within the final MP3. They are measured from the MP3 frames of its segments, so the playback speed is in them; they are contiguous and the last ends at the file's length. Older outputs have none, and a segment that cannot be measured leaves the audio without times, with a warning.
- Agent chat questions.
  - A plan asks when an open choice changes the cost or the result: status `asking`, with `plan.question` holding 2–4 choices, each with its estimate, and a preselected default. `POST /api/rag/conversations/{id}/plans/{plan_id}/answer` applies the answer to the steps and proposes the plan; confirm waits for it.
  - The first question is the judge. A judge the message asks for or against is taken as said; otherwise, with the user's `judge_default` set the plan asks, and with it off the plan says "without a judge" in a note.
  - Migration `0014_rag_plan_questions.sql`.
- Agent chat and web pages.
  - A plan creating an output from a notebook with web pages asks once whether to use the versions already read (preselected) or fetch them again, with the extra cost; a message that says it ("neu laden", "the cached pages") is not asked. `create_output` takes `web_pages`: `cached` or `fresh`.
  - Questions come one after another; `plan.answered` lists the answered ones. `plan.sources` names the notebook's sources with when a web page was last read. Migration `0015_rag_plan_sources.sql`.
  - The plan's summary message names what was made (`Created the quiz "<title>"`) and carries the plan with each step's result, to link it.
  - `Answer.web_pages` and `Message.web_pages`: the cited web pages with the time their version was read (`fetched_at`).

### Changed

- Audio is synthesized turn by turn (paragraph by paragraph): consecutive turns of one voice are no longer joined into one stretch, so each turn's place in the file can be measured. There are more requests now. The platform bills each request in whole seconds, so a script can cost up to about a second of speech more per extra request.

### Fixed

- A judge call without a usable answer no longer fails, and charges again, a whole output. It is tried once more, then the unit is kept and marked `judge.verdict: null` with the reason ("the judge gave no usable answer: <kind>"), and counted as `no_usable_answer`. The judge's output budget is 4000 tokens, up from 1500, since judge models reason before answering.
- Failure texts and the worker's WARNING line name the kind of failure ("empty answer (length)", "unparseable answer", "unknown verdict", "timeout", "HTTP 429") instead of only "ProviderError".

## [0.20.0] - 2026-10-15

### Added

- `Output.phase` (core #511): what a waiting or running output is doing now. It is one of `waiting_for_sources` (with the names still being read), `retrieving`, `deciding`, `writing`, `checking`, `judging` (round n of 3), `revising`, `synthesizing_speech` (segment n of m), `indexing` or `retry` (attempt n of 3 and the short error), with the time the step began. Each step is written when it really starts, in the same update as the claim's heartbeat, and cleared once the output is ready or failed. Migration `0013_rag_output_phase.sql`.

## [0.19.7] - 2026-10-14

### Fixed

- A selected source that could not be read is never left out silently.
  - Creating an output with one is refused with 409 `Source '<name>' could not be read: <reason>`.
  - If it fails while the output waits, the output fails with that message before any model call, uncharged.
  - A failed web page in `url_refresh` is retried instead.
  - Answers name it, and the estimate lists it under `blocked`.
- An output waiting more than 30 minutes for its sources fails, naming the source, uncharged.
- A running job refreshes its claim at most once a minute, so a long run (a judged report, a long audio) is no longer taken over after 10 minutes and run, and charged, a second time while it is still going.
- A failed attempt's error says what happens next.
  - While a retry is pending, it ends "attempt n of 3 follows".
  - A final failure says it is final and names the model and the error class: "the model provider failed 3 times (glm-5.3: timeout); no further attempt; nothing was charged" (or "the calls made before the failure were charged").
  - The worker logs every failed attempt at WARNING with output id, attempt, model, error class and message, never the prompt.

## [0.19.5] - 2026-10-14

### Added

- Audio style `faq`: 5 to 8 questions a listener would most likely ask about the material, each followed by its answer. Two voices in strict turn, the interviewer asking with `voice` and the expert answering with `audio_voice_2` (defaults as for `conversation`). The answers are cited and checked like the other styles, and the questions carry no claim. The catalogue lists the style, and `overview` stays the default.

## [0.19.0] - 2026-10-14

### Changed

- Sources are read when they are used.
  - Creating an output or asking a question first has ingest read the selected sources not read yet (`added`), with the caller's bearer. The output job waits for them; an answer waits up to `ANSWER_READ_WAIT_SECONDS` (60) and then answers from what is ready, naming the sources still being read.
  - `url_refresh` (library ids of selected web pages) has ingest check those pages again first; the others use the cache.
  - `POST /api/rag/estimate` with `notebook_id` lists reading each unread source and re-reading each refreshed page as `separate` lines, at ingest's per-source price. An agent plan with an output does the same, and its output step reads as the user too.
  - Setting `ANSWER_READ_WAIT_SECONDS`.

## [0.18.6] - 2026-10-14

### Fixed

- Agent chat: a reply becomes a plan only when it calls a tool that changes the notebook (`add_url`, `add_from_library`, `create_output`). A question the planner met with read tools only (`explain_output`, `list_sources`, `list_outputs`) gets the normal cited answer. On 0.18.5 the smoke test's plain question came back as a plan of one step that could not run.

## [0.18.5] - 2026-10-14

### Added

- Agent chat.
  - A message that asks for actions gets a plan instead of an answer (`Answer.plan`, `Message.plan`): `add_url`, `add_from_library`, `create_output`, `explain_output`, `list_sources` and `list_outputs`, at most six steps. Each step comes with its estimate, and an invalid step is shown with its reason. The processing of an added web page is ingest's own action, so it is a separate line (`separate`, the step's `processing`), not part of the plan's total.
  - Nothing runs before `POST /api/rag/conversations/{id}/plans/{plan_id}/confirm` (`/cancel`, `GET` for polling). Ingest steps run in the confirm request with the user's bearer, outputs through the queue, and explanations once their output is ready. An output waiting longer than 30 minutes for a source fails uncharged.
  - The planner sees no passage. A URL must be in the user's message, and library sources come from the user's library.
  - One `action_id` per plan. Migration `0012_rag_plans.sql`, setting `INGEST_URL`.

- Output kind `flashcards`: `{"cards": [{"front", "back", "hint"?, "refs", "support"}]}`. Options `card_count` (5–50, default 15), `difficulty` and `audience` as the quiz, `instructions` and `judge`. Each card is one unit: its back is the claim, checked with its front like a quiz answer (`lexical-overlap-v1`, the judge when chosen). Passages are balanced across sources, and provenance, the catalogue entry and the estimate work as for the other kinds. Migration `0011_rag_flashcards_kind.sql`.

- Suggested questions.
  - Every chat answer returns `suggestions`: up to three follow-up questions written in the same model call as a `Suggestions:` tail, cut off the answer and stored on the message.
  - `GET /api/rag/notebooks/{id}/suggestions`: a notebook's starting questions from one call over its source summaries, billed as a `chat_answer` action, cached until its ready uploads change. Estimate: `{"action": "chat_answer", "options": {"suggestions": true}}`.
  - Migration `0010_rag_suggestions_feedback.sql`.

- Feedback on answers: `POST /api/rag/messages/{id}/feedback` `{"rating": "up" | "down", "comment"?}` (the owner's, one per answer, changeable, on `Message.feedback`). `GET /api/rag/admin/feedback` (admin read) gives ratings per day and the latest down-rated answers with their question.

- Starter notebook outputs. Ingest queues an `output_copies` row (migration `0009_rag_output_copies.sql`) with its id maps when it copies the starter notebook for a new user. The worker copies the starter's ready outputs into the copy: rows owned by the new user, generated library rows with links and chunks, and audio files under new keys. Provenance and option ids are rewritten through the maps, and what cannot be mapped is dropped. Share tokens are not copied; judge annotations are kept. There is no model call and no charge, and `outputs.copied_from` makes it idempotent.

### Security

- `GET /api/rag/admin/feedback` gives a viewer the per-day counts only: `latest_down` is empty and `"detail": "admins only"`, because the answers and questions are the users' own words. Admins see them as before.

## [0.18.0] - 2026-10-14

### Added

- `POST /api/rag/estimate`: the charge range of an output (`kind` with its options, a judge level included) or of an action (`chat_answer`, `chart_propose`, `judge`, `voice_sample`) before it runs. Each call the action makes is priced at assumed low and high token counts with core's cost in US cents, converted at the pricing rules' `usd_per_eur` and charged in euro cents with the action's margin; the action's fixed fee is counted once. Without an exchange rate every request that would call a model answers 503 before any provider call, and the worker claims no job.

- `options.judge` (`off` default, `low`, `medium`, `high`) for every kind but `chart`. Each claim unit is judged by the level's model (`JUDGE_MODELS`, required to be priced) after the word check. Failing units are revised in one call of the author model and judged again, for at most three rounds; what still fails is removed and listed in `provenance.judge.removed`. Units carry `judge {level, model, verdict, reason, quote, round}`, and `provenance.judge` logs every round with its usage.

- `POST /api/rag/outputs/{id}/judge` and `POST /api/rag/messages/{id}/judge` `{"level"}` judge an existing output or chat answer on request: annotate only, nothing revised or removed; 409 without cited passages.

- The judge's single call (`lasteverlm_rag/judge.py`, prompt `judge-v1`): one claim and its cited passages in, a verdict `supported` | `contradicted` | `not_in_source` with a one-sentence reason and the decisive quote out. The answer is parsed deterministically; a malformed one is a billed `ProviderError`. Not yet used by generation.

- The judge evaluation (`evals/judge/`, `make judge-eval`, `docs/judge-eval.md`): 24 hand-written claims on demo-set passages in six categories, judged three times per level against the platform, beside the word check as the baseline, with hit rate, cost and latency per call. The first results are committed (`results/2026-10-14.*`), and the level-to-model mapping is chosen from them.

### Changed

- Every usage row is booked through core's `record_usage` under its user action, with one `action_id` per action: `chat_answer`; `output:<kind>` under the output's id (all attempts, TTS segments, both calls of a decision, judge rounds and revisions); `chart_propose`; `judge` on request; `voice_sample`. Core fixes the cost (US and euro cents) and the charge (euro cents) and books a fixed fee once per action, and the credit check runs over the charges. rag computes no cents of its own.

## [0.17.0] - 2026-10-14

### Added

- Output kind `chart` in two steps.
  - `POST /api/rag/notebooks/{id}/charts/propose` (`instructions`, `max` 1–12, default 6) returns candidate charts from one LLM call over numeric passages and stores nothing. The chart types are `bar`, `line`, `pie`, `stacked_bar` and `scatter`. When the sources hold no number series, the list is empty with a reason.
  - A `chart` output is created from the picked candidates (`options.charts`) without a second model call. Its cited chunks are loaded again from the notebook, and every value is checked strictly (`number-match-v1`): a value is `supported` only when the number is written in a cited passage, a computed value is `derived`, and anything else is `unsupported`.
  - Migration `0008_rag_chart_kind.sql`. The catalogue lists `chart`.
- Report options `report_style` (`briefing` default, `detailed`, `executive_summary`, `explainer`, `academic`) and `length` (`short`, `medium` default, `long`); quiz options `question_count` (5–30, default 10), `difficulty` (`easy`, `medium` default, `hard`) and `audience` (`children`, `teens`, `adults` default, `experts`). Defaults are stored on the output; the catalogue lists them under `report` and `quiz`. A quiz with fewer questions than asked gets the corrective retry, extra ones are cut.

- `instructions` for every kind: the user's own notes (at most 500 characters, whitespace collapsed, blank dropped), given to the generator below the system rules; they steer focus and tone and never override the rules, the citation rules or the reply format. Stored in the options and the provenance log; the catalogue lists `instructions.max_chars`.

- `PATCH /api/rag/outputs/{id}` `{"title"}` renames a ready output outside the trash (trimmed, 1–200 characters) together with its generated source; 409 while it is still being generated.

- `GET /api/rag/shared/notebooks/{token}` (no auth): a notebook shared through ingest's `notebooks.share_token`, as its title, its ready uploads by name and kind, and its ready outputs outside the trash as their shared view; 404 for an unknown or revoked token.

## [0.15.0] - 2026-10-14

### Added

- Output kind `decision`: a question (`question`, `decision_type` `yes_no` | `choice` with `choices` | `scale` with `levels`) answered by the platform's decision model (`DECISION_MODEL`, default `clef`, which needs a price) over the retrieved passages, with evidence for and against, gaps and a short verdict written independently by the LLM from the same passages; `agreement` (`consistent`, `conflicting`, `unclear`, `undecided`) compares the two, and without supported evidence the verdict is "The sources do not decide this.". Migration `0007_rag_decision_kind.sql`; the provenance log lists both calls.

## [0.14.0] - 2026-10-14

### Added

- Chat answers get the outputs' support check: `Answer.units` (one `{"text", "refs", "support"}` per paragraph) and `Answer.check` (the counts, method `lexical-overlap-v1`), stored with the assistant message (migration `0006_rag_message_support.sql`) and returned on `Message`; `null` for an answer without citations. An answer in another language than its passages marks low overlap `unverified`.

- Provenance for table, quiz and mind map: `content.row_refs` (one `{"refs", "support"}` per table row), `refs` and `support` on every quiz question (checked with its answer and explanation) and on every mind-map node below the root; the same check and log as the other kinds.

- Provenance for report, infographic and audio: every unit (report paragraph, infographic fact and section, audio paragraph or turn) carries `refs` (the passage numbers it cites) and `support` (`supported`, `unsupported` or `unverified`) from a deterministic word-overlap check after generation (`lexical-overlap-v1`, threshold 0.5, no model call); failing units are kept and marked, never dropped. The report keeps `[n]` markers in its markdown and lists `content.units`. `Output.provenance` (migration `0005_rag_output_provenance.sql`, core's model) is the generation log: options, retrieval candidates and which entered the prompt, the numbered passages with their text, model, prompt version, usage, timestamps and the check's counts; the shared view carries it without ids.

### Changed

- `/healthz` reports `dependencies`: `postgres`, `storage` (when configured) and `inference`, each `ok` or `down`, through core's `DependencyProbe` (2 s per check, cached 15 s); `status` stays `ok` when one is down.

## [0.13.0] - 2026-10-14

### Added

- Audio styles: option `audio_style` (`overview`, the default and the previous behaviour; `story`, the material told as a narrative by one narrator; `conversation`, an interviewer and an expert grounded in the material) with a prompt and a JSON schema per style, and `audio_voice_2` for the expert (default: another voice with the first one's native language). Each turn is read with its speaker's voice and billed per second as before; the audio content carries `transcript` (`[{"speaker", "text"}]`), also on the shared view. The options catalogue lists `styles` and `default_style`.

- Outputs in the user's library: `GET /api/rag/library/outputs?kind=&trash=` lists the caller's outputs from every notebook, including deleted notebooks; `Output` carries `origin_title` (the notebook's title when the output was made), `deleted_at` and `purge_at` (core's model). The generated source of an output is the user's library row, which ingest can link into another notebook without reprocessing.
- A trash for outputs: `POST /api/rag/outputs/{id}/restore`, `DELETE /api/rag/outputs/{id}/purge` (409 unless trashed), `DELETE /api/rag/notebooks/{id}/outputs` (trashes every output made in that notebook). The worker purges trashed outputs after the owner's `trash_days` (default 14), at start and then hourly: the stored file first, then the generated source, then the row; a file that cannot be deleted is logged and kept for the next sweep.
- Migration `0004_rag_outputs_library.sql`: `outputs.owner`, `origin_title`, `deleted_at`; `notebook_id` nullable with `on delete set null`; `source_id` references the generated library row (expand only, `owner` stays nullable for the rolling deploy).

### Changed

- `DELETE /api/rag/outputs/{id}` moves the output to the trash instead of deleting it; the output and its generated source leave every notebook, `GET` and the share link answer 404 until it is restored.
- An output survives the deletion of its notebook (`notebook_id` becomes `null`); a job whose notebook is gone fails with "the notebook was deleted".
- `DELETE /api/rag/admin/users/{id}` deletes the user's outputs and generated sources by owner, so outputs of deleted notebooks and trashed ones go too.

## [0.12.0] - 2026-10-14

### Added

- `DELETE /api/rag/admin/users/{id}` (admins only), account's first step when a user is deleted: the stored files of the outputs in the user's notebooks, then rag's rows there (generated sources, outputs, messages, conversations) in one transaction. Idempotent, always 204; a file that cannot be deleted is a 502 naming its key, with every row kept.

## [0.11.1] - 2026-10-14

### Fixed

- Text to speech is billed per second of generated speech, as the platform reports and charges it (core's `SpeechResult`), instead of per input character, which charged about 14 times the platform's price (100 characters: 2.8 cents against 7 seconds at 0.196 cents); `MODEL_PRICES` for `TTS_MODEL` is read as cents per million seconds.

## [0.10.0] - 2026-10-14

### Added

- Source selection: `source_ids` on `/ask` and on `POST .../outputs` (link ids of the notebook's uploads; absent or empty means all, any other id is a 422). Chat retrieves only from the selected uploads and its document list marks the others `not in scope`; an output is generated from the selected sources only and records them as `options.sources`.

### Changed

- The Docker image builds on `python:3.13-slim` pinned by digest (refresh steps in the Dockerfile).
- `GET /api/rag/admin/stats` and `/admin/stats/daily` admit viewers as well as admins (core's `admin_read` over `users.role`); a viewer is a plain user everywhere else.
- The worker gives each inference request at least 180 s (`LLM_TIMEOUT_SECONDS` from core, default 60, can raise it); the API keeps the configured value.

### Fixed

- A model or embedding call that the provider billed but that still failed (an empty answer, an unusable vector) writes its usage row before the error propagates, in chat, generation and indexing.
- An output with a source selection waits only for the selected sources to finish processing, not for every upload of the notebook.
- A failure while cleaning up after a failed generation attempt (removing the generated source or the audio file) is logged on its own and no longer replaces the attempt's error.
- A text-to-speech answer without audio (output or voice preview) is a provider failure instead of an empty MP3; its usage row is written before the check.

### Security

- Prompts treat text from the user's files as data: both system prompts say so, names are one bounded line, and marker-shaped text in sources and summaries is rewritten (`[2]` → `(2)`) so a source cannot forge a passage or a citation. The coverage warning logs a bounded name.
- `Output.error` no longer carries provider, storage or internal exception messages (they can name endpoints or quote model output): rag's own reasons are shown as they are, everything else as a fixed text per class, with the detail in the log. A schema failure no longer quotes the validation error.

## [0.9.0] - 2026-10-13

### Added

- `PATCH /api/rag/conversations/{id}` renames a chat tab: `{"title"}`, trimmed, 1–80 characters.
- The chat's system prompt lists the notebook's ready documents by kind and title, uploads and generated, so "which documents can you see?" or "the mind map you made" is answered; the model is called even when no passage matches, and only an empty notebook gets the fixed refusal.

### Changed

- An unscoped `/ask` retrieves generated documents again, bounded: the uploads fill the k passages by score, generated chunks above the floor may take at most `RETRIEVAL_GENERATED_SLOTS` (default 2) of them and are listed after the uploads. `RETRIEVAL_INCLUDE_GENERATED` is gone.

## [0.7.3] - 2026-10-13

### Fixed

- A generated document could cover one source only: the generator's passages were a global top-k that one source could fill. Retrieval for generators now keeps each ready source's best chunks and fills the rest by score; the prompts require every source to be covered (the mind map's root spans them), and a source the document never mentions is logged as a warning.

## [0.7.1] - 2026-10-13

### Changed

- `/healthz` answers core's `Health` (`status`, `version`, `commit` from `APP_VERSION` / `APP_COMMIT`), so the admin system view shows rag's build.

## [0.7.0] - 2026-10-13

### Added

- `POST /api/rag/outputs/voice-sample`: a short preview of a voice at a speed and in a language, synthesized once and then served from storage.

## [0.6.0] - 2026-10-13

### Added

- Citations carry their provenance: `similarity` (the chunk's score for the question), `chunk_index` and `chunk_count`; persisted messages keep them.
- `GET /api/rag/chunks/{chunk_id}`: the exact chunk behind a citation, for its owner.
- Output options: `language` for every kind, `voice` and `speed` for audio, validated per kind, stored on the output (migration `0003_rag_output_options.sql`) and returned as `options`; `GET /api/rag/outputs/options` serves the catalogue.
- `GET /api/rag/admin/stats/daily?days=30` for the admin charts: messages and outputs (total and by kind) per UTC day, every day present.

### Changed

- A citation's `quote` is the verbatim chunk text; it was cut at 300 characters before.
- `TTS_VOICE` must be one of the platform's voices; startup refuses any other.

## [0.5.0] - 2026-10-13

### Changed

- Retrieval, the output queue's gate and the generator input read sources through ingest's source library (`sources` link → `library_sources` → `chunks`); the source id in answers stays the link's.
- A generated output is indexed as a hidden `generated` library row plus its link, created in one transaction and deleted with the output.

## [0.4.0] - 2026-10-13

### Added

- Conversations: several chats per notebook (`GET`/`POST /api/rag/notebooks/{id}/conversations`, `DELETE /api/rag/conversations/{id}`, `GET /api/rag/conversations/{id}/messages`); migration `0002_rag_conversations.sql` moves existing history into `Chat 1`.
- The model sees the newest six messages of the conversation, so a follow-up question can refer to an earlier turn.

### Changed

- `POST .../ask` requires `conversation_id` (422 without it); `GET /api/rag/notebooks/{id}/messages` is removed in favour of the per-conversation list.
- Admins need credit like every user again (core's `has_credit` dropped the admin exemption); README and tests follow.

## [0.3.0] - 2026-10-13

### Added

- `GET /api/rag/admin/stats` for admins: notebooks, messages (total, last 24 h), outputs by kind and by status, jobs pending, running and failed, shared outputs.

### Changed

- The credit check is core's `has_credit`: admins have unlimited credit; everyone else still gets 402 at a zero balance.

## [0.2.1] - 2026-10-13

### Changed

- Formatting is black's (line length 100); `make lint` runs `black --check` and keeps `ruff check` for linting.

## [0.2.0] - 2026-10-13

### Added

- Audio output: an LLM-written spoken-word script, synthesized with `TTS_MODEL` and `TTS_VOICE` (default `ryan`) in sentence-boundary segments, stored as one MP3 in object storage and served as a presigned URL valid for one hour; one `tts` usage row per segment.

### Changed

- Deleting an output also deletes its stored files.

## [0.1.0] - 2026-10-12

### Added

- Core's rate-limit middleware in front of every endpoint (`ip_blocks`, `ip_bans`, `access_log` per the architecture).
- Answer generation over an OpenAI-compatible inference API with citations mapped back to source, chunk and page; markers naming no passage are stripped.
- Bearer token verification (JWT or shared demo token), notebook ownership check, 402 before any model call when the caller has no credit.
- Database migration `db/migrations/0001_rag.sql` for `messages` and `outputs`.
- FastAPI service for the `/api/rag` prefix: ask, messages, outputs, sharing, `/healthz` at the root and under the prefix.
- Fixed refusal with empty citations when no chunk passes the similarity floor; the model is not called.
- Generated documents: report, quiz, table, mind map, infographic through one `OutputGenerator` interface; audio prepared and failing clearly until text-to-speech exists.
- Indexing of a ready output as a source of kind `generated` in its notebook, so chat can cite it; `scope.output_id` on `/ask` restricts retrieval to it.
- Multi-stage Dockerfile with a non-root user; `make check`, `make run`, `make worker`, `make build`.
- One priced `usage` row per embedding and LLM call; startup refuses a configured model without a price.
- Retrieval over `chunks` with pgvector, scoped to one notebook and ready sources, with a configurable similarity floor.
- Sharing of a ready output through a revocable token and a read-only endpoint without authentication.
- Worker entrypoint `lasteverlm-rag-worker` that claims output jobs from the `outputs` table with `FOR UPDATE SKIP LOCKED`, so API pods stay stateless and workers scale on their own.
- Unit tests with mocked store, retriever and providers for citation mapping, the refusal path, credit, pricing, the output lifecycle, sharing and the generators' schemas.

### Changed

- Citations are contract-clean: grouped markers become single `[n]` markers and `n` is renumbered densely in order of first appearance after made-up markers are dropped.
- A scoped `/ask` retrieves without a similarity floor and with k=12 over its one document, so a summary-type question is answered.
- An unscoped `/ask` retrieves from the uploaded sources only; generated documents restate them and are reached through `scope.output_id`. `RETRIEVAL_INCLUDE_GENERATED=true` restores the previous behaviour.
- Generators are told to title the document by its subject and form, never with the notebook's name (the first live run produced four documents named after the notebook).
- The worker closes its connection pool on shutdown and logs every finished output.
- A model reply without content (a reasoning model that spent its budget on thinking) is a provider failure: `/ask` answers 502 and stores no message, a generation is retried by the worker; never an empty answer.
- The LLM is constructed with `LLM_MAX_TOKENS` as its default budget and called with thinking disabled (core's default) for answers and generators; the generator call-back carries core's `complete` signature.
- Generated documents are chunked with `lasteverlm_core.chunking`, the family's one chunker; the local copy is gone.
- Shared API models, bearer-token resolution, pricing, usage accounting and the provider adapters come from `lasteverlm_core` (installed by path from `../core`); the local mirror is gone.

