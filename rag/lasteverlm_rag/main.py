"""FastAPI application. Endpoints follow the rag section of docs/architecture.md in the parent."""

import logging
import re
import uuid
from collections.abc import Mapping
from functools import partial
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from lasteverlm_core import (
    LLM,
    Answer,
    AuthError,
    ChunkView,
    Embedder,
    ExchangeRateMissingError,
    Health,
    Message,
    OpenAICompatibleEmbedder,
    OpenAICompatibleLLM,
    OpenAICompatibleTextToSpeech,
    Output,
    OutputKind,
    PostgresRateLimitStore,
    ProviderError,
    RateLimiter,
    RateLimitMiddleware,
    RateLimitStore,
    Role,
    S3Storage,
    SharedOutput,
    current_user,
    make_client,
    mask_email,
)
from lasteverlm_core.health import (
    DEPENDENCY_TIMEOUT_SECONDS,
    Check,
    DependencyProbe,
    inference_check,
    postgres_check,
    storage_check,
)
from lasteverlm_core.models import Dependency
from lasteverlm_core.web import admin_only, admin_read

from . import __version__
from .admin import admin_stats, daily_stats
from .agent_service import AgentService, PlanAsking, PlanNotFound, PlanNotOpen, StaleQuestion
from .config import Settings
from .estimate import (
    Models,
    action_calls,
    estimate,
    level_options,
    output_calls,
    output_estimator,
    step_estimator,
)
from .failures import failure_cause
from .ingest_client import HttpIngest, Ingest, IngestError
from .judge import judge_models
from .models import (
    AdminStats,
    AskRequest,
    ChartPropose,
    Conversation,
    ConversationCreate,
    ConversationRename,
    DailyStats,
    EstimateRequest,
    FeedbackRequest,
    JudgeJob,
    JudgeRequest,
    NotebookSuggestions,
    OutputCreate,
    OutputRename,
    PartPropose,
    PlanAnswer,
    SharedNotebook,
)
from .options import SECOND_JUDGE, VERIFICATION_LEVELS, OutputOptions, catalogue, resolve
from .outputs import OutputGenerator, registry
from .outputs.chart import ChartProposals
from .outputs.proposals import TableProposals, ThemeProposals
from .reading import (
    InvalidRefresh,
    could_not_read,
    failed_sources,
    reading_lines,
    selected_uploads,
)
from .service import (
    CandidateOutOfScope,
    ChartOutOfScope,
    ChunkNotFound,
    ConversationNotFound,
    InsufficientCredit,
    InvalidOptions,
    InvalidSources,
    JudgeJobNotFound,
    JudgementRunning,
    MessageNotFound,
    NotAnAnswer,
    NotebookNotFound,
    NothingToJudge,
    ObjectDeleteFailed,
    OutputNotFound,
    OutputNotInTrash,
    OutputNotReady,
    OutputService,
    RagService,
    SourceUnreadable,
    check_sources,
    require_notebook,
)
from .store import (
    PgVectorRetriever,
    PostgresStore,
    Retriever,
    Store,
    core_connect,
    make_pool,
)
from .voice_samples import SpeechUnavailable, VoiceSampleRequest, VoiceSamples

PREFIX = "/api/rag"

# the admin output search: 6 to 32 hex digits of an id, at most this many matches
ID_PREFIX = re.compile(r"[0-9a-f]{6,32}")
ADMIN_OUTPUTS_MAX = 20
# how many down-rated answers the admin overview lists
FEEDBACK_LATEST = 20
log = logging.getLogger("lasteverlm_rag")

NotebookId = Annotated[uuid.UUID, Path()]
OutputId = Annotated[uuid.UUID, Path()]
ConversationId = Annotated[uuid.UUID, Path()]
ChunkId = Annotated[uuid.UUID, Path()]
MAX_DAILY_DAYS = 365


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    retriever: Retriever | None = None,
    embedder: Embedder | None = None,
    llm: LLM | None = None,
    generators: dict[OutputKind, OutputGenerator] | None = None,
    output_service: OutputService | None = None,
    rate_limit_store: RateLimitStore | None = None,
    voice_samples: VoiceSamples | None = None,
    dependency_probe: DependencyProbe | None = None,
    ingest: Ingest | None = None,
    judges: Mapping[str, LLM] | None = None,
) -> FastAPI:
    """Build the app. Dependencies can be injected for tests; otherwise they come from settings."""
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level)
    # /healthz reports the state of what this app built itself
    checks: dict[Dependency, Check] = {}

    if store is None or retriever is None or rate_limit_store is None:
        pool = make_pool(settings.database_url)
        checks["postgres"] = postgres_check(
            lambda: pool.connection(timeout=DEPENDENCY_TIMEOUT_SECONDS)
        )
        store = store or PostgresStore(pool, settings.model_prices)
        retriever = retriever or PgVectorRetriever(pool)
        rate_limit_store = rate_limit_store or PostgresRateLimitStore(partial(core_connect, pool))
    if embedder is None or llm is None:
        client = make_client(settings.inference)
        checks["inference"] = inference_check(client)
        embedder = embedder or OpenAICompatibleEmbedder(
            client, settings.inference.embedding_model, settings.inference.embedding_dimension
        )
        llm = llm or OpenAICompatibleLLM(
            client, settings.inference.llm_model, max_tokens=settings.inference.llm_max_tokens
        )
    generators = generators or registry(tts_available=settings.tts_model is not None)

    ingest = ingest or HttpIngest(settings.ingest_url)
    # one set of judge models: a chat answer's verification, outputs judged on request
    if judges is None:
        judges = judge_models(
            make_client(settings.inference),
            {**settings.inference.judge_models, SECOND_JUDGE: settings.second_judge_model},
        )
    rag = RagService(
        store,
        retriever,
        embedder,
        llm,
        settings.retrieval_k,
        settings.similarity_floor,
        generated_slots=settings.retrieval_generated_slots,
        ingest=ingest,
        read_wait_s=settings.answer_read_wait_s,
        judges=judges,
    )
    objects = S3Storage(settings.s3) if settings.s3 else None
    if objects is not None:
        checks["storage"] = storage_check(objects)
    probe = dependency_probe or (DependencyProbe(checks) if checks else None)
    models = Models(
        llm=settings.inference.llm_model,
        embedding=settings.inference.embedding_model,
        decision=settings.inference.decision_model,
        tts=settings.tts_model,
        judges={
            **{str(k): v for k, v in settings.inference.judge_models.items()},
            SECOND_JUDGE: settings.second_judge_model,
        },
    )
    # outputs are synthesized by the worker; the API presigns and deletes their files
    outputs = output_service or OutputService(
        store,
        retriever,
        embedder,
        llm,
        generators,
        settings.retrieval_k,
        settings.similarity_floor,
        objects=objects,
        default_voice=settings.tts_voice,
        # for judging an existing output or answer on request
        judges=judges,
        ingest=ingest,
        read_wait_s=settings.answer_read_wait_s,
        output_max=output_estimator(settings.model_prices, store.pricing_rules, generators, models),
    )
    agent = AgentService(
        store,
        rag,
        outputs,
        llm,
        ingest,
        step_estimator(
            settings.model_prices, store.pricing_rules, generators, models, ingest.url_charge_cents
        ),
        settings.tts_voice,
    )
    # the one synthesis the API does itself: short, cached voice previews
    samples = voice_samples or VoiceSamples(
        store,
        (
            OpenAICompatibleTextToSpeech(
                make_client(settings.inference), settings.tts_model, voice=settings.tts_voice
            )
            if settings.tts_model
            else None
        ),
        objects,
    )

    app = FastAPI(title="lasteverLM rag", version=__version__, docs_url=None, redoc_url=None)
    # the worker's half of the agent chat runs elsewhere; tests drive it through this
    app.state.agent = agent
    app.state.outputs = outputs
    _install_error_handlers(app)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=RateLimiter(rate_limit_store, settings.rate_limit),
        service="rag",
        auth=settings.auth,
    )

    def user_id(authorization: Annotated[str | None, Header()] = None) -> str:
        """The caller's user id as a string; ids stay strings inside this service
        and are cast to UUID at the store boundary."""
        try:
            return str(current_user(authorization, settings.auth).user_id)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    User = Annotated[str, Depends(user_id)]  # noqa: N806

    def principal(authorization: Annotated[str | None, Header()] = None) -> uuid.UUID:
        return uuid.UUID(user_id(authorization))

    def role_lookup(_: Request, uid: uuid.UUID) -> Role:
        assert store is not None
        return store.admin_role(str(uid))

    # rag's admin endpoints only read: viewers and admins pass, 401 from the
    # principal, 403 "admin only" from core for a plain user
    AdminReader = Annotated[uuid.UUID, Depends(admin_read(principal, role_lookup))]  # noqa: N806
    # ... and the one that changes something is for admins only
    AdminWriter = Annotated[  # noqa: N806
        uuid.UUID,
        Depends(admin_only(principal, lambda r, uid: role_lookup(r, uid) == "admin")),
    ]

    build = Health.from_env()  # the build is fixed for the life of the process

    @app.get("/healthz", response_model=Health)
    @app.get(f"{PREFIX}/healthz", response_model=Health)
    async def healthz() -> Health:
        # cached for 15 s, 2 s per check; a dependency that is down is
        # reported, the probe itself stays "ok"
        if probe is None:
            return build
        return build.model_copy(update={"dependencies": await probe.states()})

    @app.post(f"{PREFIX}/notebooks/{{notebook_id}}/ask", response_model=Answer)
    def ask(
        notebook_id: NotebookId,
        body: AskRequest,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Answer:
        """An answer, or a plan when the message asks for actions (agent chat)."""
        output_id = body.scope.output_id if body.scope else None
        return agent.ask(
            user,
            str(notebook_id),
            body.question,
            str(body.conversation_id),
            authorization,
            output_id,
            [str(s) for s in body.source_ids or []],
            [str(u) for u in body.url_refresh or []],
        )

    plan_path = f"{PREFIX}/conversations/{{conversation_id}}/plans/{{plan_id}}"

    @app.get(plan_path)
    def get_plan(conversation_id: ConversationId, plan_id: uuid.UUID, user: User) -> dict[str, Any]:
        """The plan with every step's state, for polling while it runs."""
        return agent.get(user, str(conversation_id), str(plan_id))

    @app.post(f"{plan_path}/confirm")
    def confirm_plan(
        conversation_id: ConversationId,
        plan_id: uuid.UUID,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Run a proposed plan: ingest steps now with this request's token, the rest
        through the worker."""
        return agent.confirm(user, str(conversation_id), str(plan_id), authorization or "")

    @app.post(f"{plan_path}/answer")
    def answer_plan(
        conversation_id: ConversationId,
        plan_id: uuid.UUID,
        body: PlanAnswer,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """The user's answer to the plan's question; the plan is then proposed."""
        return agent.answer(
            user,
            str(conversation_id),
            str(plan_id),
            body.question_id,
            body.choice,
            body.text,
            authorization,
        )

    @app.post(f"{plan_path}/cancel")
    def cancel_plan(
        conversation_id: ConversationId, plan_id: uuid.UUID, user: User
    ) -> dict[str, Any]:
        return agent.cancel(user, str(conversation_id), str(plan_id))

    @app.get(f"{PREFIX}/notebooks/{{notebook_id}}/conversations", response_model=list[Conversation])
    def list_conversations(notebook_id: NotebookId, user: User) -> list[Conversation]:
        return rag.conversations(user, str(notebook_id))

    @app.post(
        f"{PREFIX}/notebooks/{{notebook_id}}/conversations",
        response_model=Conversation,
        status_code=201,
    )
    def create_conversation(
        notebook_id: NotebookId, user: User, body: ConversationCreate | None = None
    ) -> Conversation:
        return rag.create_conversation(user, str(notebook_id), body.title if body else None)

    @app.patch(f"{PREFIX}/conversations/{{conversation_id}}", response_model=Conversation)
    def rename_conversation(
        conversation_id: ConversationId, body: ConversationRename, user: User
    ) -> Conversation:
        """Renaming a chat tab."""
        return rag.rename_conversation(user, str(conversation_id), body.title)

    @app.delete(f"{PREFIX}/conversations/{{conversation_id}}", status_code=204)
    def delete_conversation(conversation_id: ConversationId, user: User) -> Response:
        """Closing a chat tab: the conversation and its messages are gone."""
        rag.delete_conversation(user, str(conversation_id))
        return Response(status_code=204)

    @app.get(f"{PREFIX}/chunks/{{chunk_id}}", response_model=ChunkView)
    def get_chunk(chunk_id: ChunkId, user: User) -> ChunkView:
        """The exact passage behind a citation, for its owner."""
        return rag.chunk(user, str(chunk_id))

    @app.get(f"{PREFIX}/conversations/{{conversation_id}}/messages", response_model=list[Message])
    def messages(conversation_id: ConversationId, user: User) -> list[Message]:
        return rag.messages(user, str(conversation_id))

    @app.post(f"{PREFIX}/notebooks/{{notebook_id}}/outputs", response_model=Output, status_code=202)
    def create_output(
        notebook_id: NotebookId,
        body: OutputCreate,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Output:
        """Inserts the job; a worker (``lasteverlm_rag.worker``) picks it up. The
        sources it needs that are not read yet are read first, as the caller."""
        return outputs.create(
            user,
            str(notebook_id),
            body.kind,
            body.options,
            [str(s) for s in body.source_ids or []],
            bearer=authorization,
            url_refresh=[str(u) for u in body.url_refresh or []],
        )

    @app.post(f"{PREFIX}/notebooks/{{notebook_id}}/charts/propose", response_model=ChartProposals)
    def propose_charts(
        notebook_id: NotebookId,
        body: ChartPropose,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ChartProposals:
        """Candidate charts from the selected sources; nothing is stored until
        the user creates a chart output. Unread selected sources are read
        first, as the caller."""
        instructions = " ".join((body.instructions or "").split()) or None
        return outputs.propose_charts(
            user,
            str(notebook_id),
            instructions,
            body.max,
            [str(s) for s in body.source_ids or []] or None,
            bearer=authorization,
            url_refresh=[str(u) for u in body.url_refresh or []],
        )

    @app.post(f"{PREFIX}/notebooks/{{notebook_id}}/tables/propose", response_model=TableProposals)
    def propose_tables(
        notebook_id: NotebookId,
        body: PartPropose,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> TableProposals:
        """Candidate tables from the selected sources: previews, nothing stored."""
        found = outputs.propose_parts(
            user,
            str(notebook_id),
            "table",
            *_propose_args(body),
            bearer=authorization,
            url_refresh=[str(u) for u in body.url_refresh or []],
        )
        assert isinstance(found, TableProposals)
        return found

    @app.post(
        f"{PREFIX}/notebooks/{{notebook_id}}/infographics/propose", response_model=ThemeProposals
    )
    def propose_themes(
        notebook_id: NotebookId,
        body: PartPropose,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ThemeProposals:
        """Candidate infographic themes from the selected sources: previews, nothing stored."""
        found = outputs.propose_parts(
            user,
            str(notebook_id),
            "infographic",
            *_propose_args(body),
            bearer=authorization,
            url_refresh=[str(u) for u in body.url_refresh or []],
        )
        assert isinstance(found, ThemeProposals)
        return found

    def _propose_args(body: PartPropose) -> tuple[str | None, int, list[str] | None]:
        instructions = " ".join((body.instructions or "").split()) or None
        return instructions, body.max, [str(s) for s in body.source_ids or []] or None

    @app.post(f"{PREFIX}/outputs/voice-sample")
    def voice_sample(body: VoiceSampleRequest, user: User) -> dict[str, Any]:
        """A short preview of a voice; synthesized once per voice, speed and
        language, then served from storage."""
        try:
            return samples.sample(user, body)
        except ValueError as exc:
            raise InvalidOptions(str(exc)) from exc

    @app.get(f"{PREFIX}/outputs/options")
    def output_options(_: User) -> dict[str, Any]:
        """The options catalogue per kind: languages, voices, speeds."""
        return catalogue(settings.tts_voice, settings.inference.judge_models)

    @app.post(f"{PREFIX}/estimate")
    def estimate_charge(
        body: EstimateRequest,
        user: User,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """The charge range of an output or an action, before it runs; with a
        notebook, reading its unread selected sources as lines of their own."""
        if (body.kind is None) == (body.action is None):
            raise InvalidOptions("estimate: give either kind or action")
        try:
            if body.kind is not None:
                options = body.options
                if body.kind != "chart":
                    resolved = OutputOptions.model_validate(options)
                    options = resolve(body.kind, resolved, settings.tts_voice)
                generator = generators[body.kind]
                action = f"output:{body.kind}"
                low, high = (
                    output_calls(body.kind, options, generator, models, case) for case in (0, 1)
                )
            else:
                action = body.action or ""
                low, high = (action_calls(action, body.options, models, case) for case in (0, 1))
        except ValueError as exc:  # a pydantic ValidationError is one too
            raise InvalidOptions(str(exc)) from exc
        result = estimate(settings.model_prices, store.pricing_rules(), action, low, high)
        if body.kind is not None and body.kind != "chart":
            # the same output at every verification level, for the form to show
            result["levels"] = {}
            for level in VERIFICATION_LEVELS:
                variant = level_options(options, level)
                lo, hi = (
                    output_calls(body.kind, variant, generator, models, case) for case in (0, 1)
                )
                priced = estimate(settings.model_prices, store.pricing_rules(), action, lo, hi)
                result["levels"][level] = {
                    "min": priced["charge_cents_min"],
                    "max": priced["charge_cents_max"],
                }
        result["separate"] = []
        result["blocked"] = []
        if body.notebook_id is not None:
            # a selected source that could not be read: the output would be refused
            notebook_id = str(body.notebook_id)
            require_notebook(store, user, notebook_id)
            selected = check_sources(store, notebook_id, [str(s) for s in body.source_ids or []])
            result["blocked"] = [
                {"library_id": s.library_id, "name": s.name, "reason": could_not_read(s)}
                for s in failed_sources(
                    selected_uploads(store, notebook_id, selected),
                    [str(u) for u in body.url_refresh or []],
                )
            ]
        if body.notebook_id is not None and authorization:
            notebook_id = str(body.notebook_id)
            selected = check_sources(store, notebook_id, [str(s) for s in body.source_ids or []])
            try:
                result["separate"] = reading_lines(
                    store,
                    ingest,
                    authorization,
                    notebook_id,
                    selected,
                    [str(u) for u in body.url_refresh or []],
                )
            except InvalidRefresh as exc:
                raise InvalidSources(str(exc)) from exc
        return result

    @app.get(f"{PREFIX}/notebooks/{{notebook_id}}/outputs", response_model=list[Output])
    def list_outputs(notebook_id: NotebookId, user: User) -> list[Output]:
        return outputs.list(user, str(notebook_id))

    @app.delete(f"{PREFIX}/notebooks/{{notebook_id}}/outputs", status_code=204)
    def delete_notebook_outputs(notebook_id: NotebookId, user: User) -> Response:
        outputs.delete_notebook_outputs(user, str(notebook_id))
        return Response(status_code=204)

    @app.get(f"{PREFIX}/library/outputs", response_model=list[Output])
    def library_outputs(
        user: User, kind: OutputKind | None = None, trash: bool = False
    ) -> list[Output]:
        return outputs.library(user, kind, trash)

    @app.get(f"{PREFIX}/outputs/{{output_id}}", response_model=Output)
    def get_output(output_id: OutputId, user: User) -> Output:
        return outputs.get(user, str(output_id))

    @app.delete(f"{PREFIX}/outputs/{{output_id}}", status_code=204)
    def delete_output(output_id: OutputId, user: User) -> Response:
        outputs.delete(user, str(output_id))
        return Response(status_code=204)

    @app.post(f"{PREFIX}/outputs/{{output_id}}/restore", status_code=204)
    def restore_output(output_id: OutputId, user: User) -> Response:
        outputs.restore(user, str(output_id))
        return Response(status_code=204)

    @app.delete(f"{PREFIX}/outputs/{{output_id}}/purge", status_code=204)
    def purge_output(output_id: OutputId, user: User) -> Response:
        outputs.purge(user, str(output_id))
        return Response(status_code=204)

    @app.post(f"{PREFIX}/outputs/{{output_id}}/share")
    def share_output(output_id: OutputId, user: User) -> dict[str, str]:
        return {"share_token": outputs.share(user, str(output_id))}

    @app.delete(f"{PREFIX}/outputs/{{output_id}}/share", status_code=204)
    def unshare_output(output_id: OutputId, user: User) -> Response:
        outputs.unshare(user, str(output_id))
        return Response(status_code=204)

    @app.post(f"{PREFIX}/outputs/{{output_id}}/judge", response_model=JudgeJob, status_code=202)
    def judge_output(output_id: OutputId, body: JudgeRequest, user: User) -> JudgeJob:
        """Queue a judgement of a ready output's claims (annotate only); the
        worker runs it. Poll the job; the result lands on the output."""
        return outputs.request_judgement(user, "output", str(output_id), body.level)

    @app.get(f"{PREFIX}/outputs/{{output_id}}/judge", response_model=JudgeJob)
    def latest_output_judgement(output_id: OutputId, user: User) -> JudgeJob:
        """The output's latest judgement job, running or ended."""
        return outputs.latest_judgement(user, "output", str(output_id))

    @app.get(f"{PREFIX}/judge-jobs/{{job_id}}", response_model=JudgeJob)
    def judgement_job(job_id: uuid.UUID, user: User) -> JudgeJob:
        """A judgement job's state: queued, running (with its phase), done or failed."""
        return outputs.judge_job(user, str(job_id))

    @app.post(f"{PREFIX}/messages/{{message_id}}/feedback", response_model=Message)
    def rate_message(message_id: uuid.UUID, body: FeedbackRequest, user: User) -> Message:
        """The owner's thumbs up or down on an answer; a new rating replaces the old."""
        return rag.rate(user, str(message_id), body.rating, body.comment)

    @app.get(f"{PREFIX}/notebooks/{{notebook_id}}/suggestions", response_model=NotebookSuggestions)
    def notebook_suggestions(notebook_id: NotebookId, user: User) -> NotebookSuggestions:
        """Starting questions for a notebook, cached until its uploads change."""
        return rag.notebook_suggestions(user, str(notebook_id))

    @app.post(f"{PREFIX}/messages/{{message_id}}/judge", response_model=JudgeJob, status_code=202)
    def judge_message(message_id: uuid.UUID, body: JudgeRequest, user: User) -> JudgeJob:
        """Queue a judgement of a chat answer's paragraphs (annotate only)."""
        return outputs.request_judgement(user, "message", str(message_id), body.level)

    @app.get(f"{PREFIX}/messages/{{message_id}}/judge", response_model=JudgeJob)
    def latest_message_judgement(message_id: uuid.UUID, user: User) -> JudgeJob:
        """The answer's latest judgement job, running or ended."""
        return outputs.latest_judgement(user, "message", str(message_id))

    @app.patch(f"{PREFIX}/outputs/{{output_id}}", response_model=Output)
    def rename_output(output_id: OutputId, body: OutputRename, user: User) -> Output:
        return outputs.rename(user, str(output_id), body.title)

    @app.get(f"{PREFIX}/shared/notebooks/{{token}}", response_model=SharedNotebook)
    def shared_notebook(token: str) -> SharedNotebook:
        """A notebook its owner shared through ingest; no sign-in."""
        return outputs.shared_notebook(token)

    @app.get(f"{PREFIX}/shared/{{token}}", response_model=SharedOutput)
    def shared_output(token: str) -> SharedOutput:
        return outputs.shared(token)

    @app.get(f"{PREFIX}/admin/stats", response_model=AdminStats)
    def stats(_: AdminReader) -> AdminStats:
        assert store is not None
        return admin_stats(store)

    @app.get(f"{PREFIX}/admin/stats/daily", response_model=DailyStats)
    def stats_daily(
        _: AdminReader, days: Annotated[int, Query(ge=1, le=MAX_DAILY_DAYS)] = 30
    ) -> DailyStats:
        assert store is not None
        return daily_stats(store, days)

    @app.get(f"{PREFIX}/admin/feedback")
    def feedback(
        reader: AdminReader, days: Annotated[int, Query(ge=1, le=MAX_DAILY_DAYS)] = 30
    ) -> dict[str, Any]:
        """Ratings per day and the latest down-rated answers with their question.
        A viewer gets the counts only: the answers and questions are the users'
        own words, which may hold personal data, so they are for admins."""
        assert store is not None
        overview = store.feedback_overview(days, FEEDBACK_LATEST)
        if store.admin_role(str(reader)) != "admin":
            return {**overview, "latest_down": [], "detail": "admins only"}
        return overview

    @app.get(f"{PREFIX}/admin/outputs")
    def find_outputs(reader: AdminReader, id_prefix: str) -> dict[str, Any]:
        """Outputs by the start of their id, as a log line or a user names one
        (short ids are its first eight hex digits): status, times and every
        failed attempt. A viewer sees the owner's email masked."""
        assert store is not None
        prefix = id_prefix.replace("-", "").lower()
        if not ID_PREFIX.fullmatch(prefix):
            raise HTTPException(422, "id_prefix: at least 6 hex characters")
        found = store.find_outputs(prefix, ADMIN_OUTPUTS_MAX + 1)
        admin = store.admin_role(str(reader)) == "admin"
        return {
            "matches": [
                {
                    "id": row.id,
                    "notebook_id": row.notebook_id,
                    "notebook_title": row.origin_title,
                    "kind": row.kind,
                    "title": row.title,
                    "status": row.status,
                    "error": row.error,
                    "created_at": row.created_at,
                    "finished_at": row.finished_at,
                    "deleted_at": row.deleted_at,
                    "owner": {"id": row.owner, "email": email if admin else mask_email(email)},
                    "attempts": row.attempts,
                    "attempt_log": row.attempt_log,
                }
                for row, email in found[:ADMIN_OUTPUTS_MAX]
            ],
            "truncated": len(found) > ADMIN_OUTPUTS_MAX,
        }

    @app.delete(f"{PREFIX}/admin/users/{{target}}", status_code=204)
    def delete_user_data(target: uuid.UUID, _: AdminWriter) -> Response:
        """Account's user deletion, before ingest deletes the notebooks."""
        outputs.delete_user_data(str(target))
        return Response(status_code=204)

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Every error body is ``{"error": "<message>"}`` as the API contract requires."""

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return error(exc.status_code, str(exc.detail))

    @app.exception_handler(SpeechUnavailable)
    async def speech_unavailable(_: Request, exc: SpeechUnavailable) -> JSONResponse:
        return error(503, "text-to-speech is not available")

    @app.exception_handler(InvalidOptions)
    async def invalid_options(_: Request, exc: InvalidOptions) -> JSONResponse:
        return error(422, f"options: {exc}")

    @app.exception_handler(CandidateOutOfScope)
    async def candidate_out_of_scope(_: Request, exc: CandidateOutOfScope) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": "candidate_out_of_scope"}, status_code=409)

    @app.exception_handler(JudgementRunning)
    async def judgement_running(_: Request, exc: JudgementRunning) -> JSONResponse:
        return JSONResponse(
            {"error": "a judgement is already running", "job_id": str(exc)}, status_code=409
        )

    @app.exception_handler(JudgeJobNotFound)
    async def judge_job_not_found(_: Request, exc: JudgeJobNotFound) -> JSONResponse:
        return error(404, "no judgement")

    @app.exception_handler(ChartOutOfScope)
    async def chart_out_of_scope(_: Request, exc: ChartOutOfScope) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": "chart_out_of_scope"}, status_code=409)

    @app.exception_handler(InvalidSources)
    async def invalid_sources(_: Request, exc: InvalidSources) -> JSONResponse:
        return error(422, f"source_ids: {exc} is not a source of this notebook")

    @app.exception_handler(ChunkNotFound)
    async def chunk_not_found(_: Request, exc: ChunkNotFound) -> JSONResponse:
        return error(404, "chunk not found")

    @app.exception_handler(ConversationNotFound)
    async def conversation_not_found(_: Request, exc: ConversationNotFound) -> JSONResponse:
        return error(404, "conversation not found")

    @app.exception_handler(NotebookNotFound)
    async def notebook_not_found(_: Request, exc: NotebookNotFound) -> JSONResponse:
        return error(404, "notebook not found")

    @app.exception_handler(OutputNotFound)
    async def output_not_found(_: Request, exc: OutputNotFound) -> JSONResponse:
        return error(404, "output not found")

    @app.exception_handler(MessageNotFound)
    async def message_not_found(_: Request, exc: MessageNotFound) -> JSONResponse:
        return error(404, "message not found")

    @app.exception_handler(SourceUnreadable)
    async def source_unreadable(_: Request, exc: SourceUnreadable) -> JSONResponse:
        return error(409, str(exc))

    @app.exception_handler(IngestError)
    async def ingest_failed(_: Request, exc: IngestError) -> JSONResponse:
        # reading or refreshing sources before an output or answer: ingest refused or is down
        log.warning("ingest call failed (%s): %s", exc.status, exc)
        return error(502, exc.public_message)

    @app.exception_handler(PlanNotFound)
    async def plan_not_found(_: Request, exc: PlanNotFound) -> JSONResponse:
        return error(404, "plan not found")

    @app.exception_handler(PlanAsking)
    async def plan_asking(_: Request, exc: PlanAsking) -> JSONResponse:
        return error(409, "answer the question first")

    @app.exception_handler(StaleQuestion)
    async def stale_question(_: Request, exc: StaleQuestion) -> JSONResponse:
        return error(409, "that question is no longer open")

    @app.exception_handler(PlanNotOpen)
    async def plan_not_open(_: Request, exc: PlanNotOpen) -> JSONResponse:
        return error(409, "the plan was confirmed or cancelled already")

    @app.exception_handler(NotAnAnswer)
    async def not_an_answer(_: Request, exc: NotAnAnswer) -> JSONResponse:
        return error(409, "feedback is for answers only")

    @app.exception_handler(NothingToJudge)
    async def nothing_to_judge(_: Request, exc: NothingToJudge) -> JSONResponse:
        return error(409, "nothing to judge: no cited passages")

    @app.exception_handler(OutputNotInTrash)
    async def output_not_in_trash(_: Request, exc: OutputNotInTrash) -> JSONResponse:
        return error(409, "output is not in the trash")

    @app.exception_handler(OutputNotReady)
    async def output_not_ready(_: Request, exc: OutputNotReady) -> JSONResponse:
        return error(409, "output is not ready")

    @app.exception_handler(ObjectDeleteFailed)
    async def object_delete_failed(_: Request, exc: ObjectDeleteFailed) -> JSONResponse:
        log.error("deleting a stored object failed: %s", exc)
        return error(502, f"could not delete stored object {exc.key}")

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        # tried twice by now (``twice``): a code and a cause the web says in the user's language
        log.warning("provider failure: %s", exc)
        return JSONResponse(
            {
                "error": "the model did not answer",
                "code": "model_no_answer",
                "cause": failure_cause(exc),
            },
            status_code=502,
        )

    @app.exception_handler(ExchangeRateMissingError)
    async def no_exchange_rate(_: Request, exc: ExchangeRateMissingError) -> JSONResponse:
        # nothing can be booked or estimated: a configuration gap, loud in the log
        log.error("%s", exc)
        return error(503, "pricing has no exchange rate, ask an admin")

    @app.exception_handler(InsufficientCredit)
    async def credit_exhausted(_: Request, exc: InsufficientCredit) -> JSONResponse:
        if exc.needed_cents is None:
            return error(402, "credit exhausted")
        # an output or a plan: what it needs at most, what started work holds,
        # and the balance, for the web to say "needs up to €x, you have €y"
        return JSONResponse(
            {
                "error": "credit does not cover this",
                "code": "credit_insufficient",
                "needed_cents": float(exc.needed_cents),
                "held_cents": float(exc.held_cents or 0),
                "available_cents": float(exc.available_cents or 0),
            },
            status_code=402,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
        return error(422, message)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return error(500, "internal error")


def app() -> FastAPI:
    """Factory for ``uvicorn lasteverlm_rag.main:app --factory``."""
    return create_app()
