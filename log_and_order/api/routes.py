"""HTTP routes. Parsing and enrichment stay in the existing pipeline."""

import asyncio
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from ..config import server_label
from ..logs.jenkins.text import redact
from ..pipeline import FormatError, incident_payload
from .ollama import OllamaStatus, check_llama, check_ollama, describe_target
from .schemas import ErrorBody, HealthBody, ModelsBody, ReadyBody
from .settings import ApiSettings, model_origin

logger = logging.getLogger("log_and_order.api")
router = APIRouter()
READ_CHUNK = 1024 * 1024


class ApiError(Exception):
    """A response with a stable error code. The body never includes log text."""

    def __init__(self, status: int, error: str, detail: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.error = error
        self.detail = detail
        self.headers = headers or {}


def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    """Render ApiError. The body is the error code and a short explanation."""
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.error, "detail": exc.detail},
        headers=exc.headers,
    )


def require_auth(request: Request) -> None:
    """Bearer token when a key is configured. Health and ready do not call this."""
    settings: ApiSettings = request.app.state.settings
    if not settings.api_key:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token, settings.api_key):
        raise ApiError(
            401,
            "unauthorized",
            "Authentication required.",
            {"WWW-Authenticate": "Bearer"},
        )


def source_name(filename: str | None) -> str:
    """Basename used only as the report label. It is never opened as a path."""
    if not filename or "\0" in filename:
        return "upload.log"
    cleaned = filename.replace("\\", "/").split("/")[-1].strip()
    if not cleaned or cleaned in {".", ".."}:
        return "upload.log"
    return cleaned


def redact_upload(text: str) -> str:
    """Redact each line in place so later line numbers still match the upload."""
    newline = text.endswith("\n")
    redacted = "\n".join(redact(line) for line in text.splitlines())
    if newline:
        redacted += "\n"
    return redacted


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read the upload in chunks and refuse anything past the configured limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await upload.read(READ_CHUNK)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ApiError(413, "payload_too_large", "The file is larger than the configured limit.")
        chunks.append(block)
    return b"".join(chunks)


def analyse_upload(name: str, text: str, settings: ApiSettings) -> dict:
    """Run the shared JSON incident pipeline. Blocking, so the route uses a thread."""
    return incident_payload(
        Path(name),
        text,
        use_llm=True,
        model=settings.model,
        base_url=model_origin(settings),
        timeout=settings.ollama_timeout,
        backend=settings.backend,
        api_key=settings.llama_api_key if settings.backend == "llamacpp" else None,
    )


def probe(
    settings: ApiSettings,
    factory: Callable[[], httpx.Client],
    *,
    list_models: bool = False,
) -> OllamaStatus:
    """One readiness check. The client comes from the app so tests can replace it."""
    with factory() as client:
        if settings.backend == "llamacpp":
            return check_llama(
                settings.llama_base_url,
                client=client,
                api_key=settings.llama_api_key,
                list_models=list_models,
            )
        return check_ollama(settings.ollama_base_url, settings.model, client=client)


def model_unavailable(settings: ApiSettings, status: OllamaStatus) -> ApiError | None:
    """503 when the model list cannot be read. A missing Ollama tag still returns names."""
    if settings.backend == "llamacpp" and status.reason == "loading":
        return ApiError(503, "llama_unavailable", "The llama.cpp server is still loading its model.")
    if status.reason == "unreachable":
        if settings.backend == "llamacpp":
            return ApiError(503, "llama_unavailable", "The configured llama.cpp server is not reachable.")
        return ApiError(503, "ollama_unavailable", "The configured Ollama server is not reachable.")
    return None


@router.get("/health", response_model=HealthBody)
async def health() -> dict[str, str]:
    """Process liveness. This does not call Ollama."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyBody, responses={503: {"model": ReadyBody}})
async def ready(request: Request) -> JSONResponse:
    """Whether the configured model server can answer. This does not run a completion."""
    settings: ApiSettings = request.app.state.settings
    status = await asyncio.to_thread(probe, settings, request.app.state.client_factory)
    body = {"status": "ready" if status.ok else "not_ready", "model": settings.model, "reason": status.reason}
    if not status.ok:
        logger.warning(
            "%s not ready reason=%s target=%s model=%s",
            server_label(settings.backend),
            status.reason,
            describe_target(model_origin(settings)),
            settings.model,
        )
    code = 200 if status.ok else 503
    return JSONResponse(status_code=code, content=body)


@router.get(
    "/models",
    response_model=ModelsBody,
    responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def models(request: Request) -> dict[str, list[str]]:
    """Model names reported by the configured model server."""
    require_auth(request)
    settings: ApiSettings = request.app.state.settings
    status = await asyncio.to_thread(
        probe,
        settings,
        request.app.state.client_factory,
        list_models=True,
    )
    unavailable = model_unavailable(settings, status)
    if unavailable is not None:
        raise unavailable
    return {"models": status.models}


@router.post(
    "/analyze",
    responses={
        400: {"model": ErrorBody},
        401: {"model": ErrorBody},
        413: {"model": ErrorBody},
        422: {"model": ErrorBody},
        500: {"model": ErrorBody},
    },
)
async def analyze(
    request: Request,
    file: UploadFile | None = File(default=None),
) -> JSONResponse:
    """Accept one log upload and return the JSON incident document."""
    require_auth(request)
    settings: ApiSettings = request.app.state.settings
    if file is None:
        raise ApiError(400, "bad_request", "A multipart file field named file is required.")
    try:
        raw = await read_limited(file, settings.max_upload_bytes)
    finally:
        await file.close()
    if b"\0" in raw:
        raise ApiError(400, "bad_request", "The file looks like a binary file.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(400, "bad_request", "The file is not valid UTF-8.") from exc
    if not text.strip():
        raise ApiError(400, "bad_request", "The file is empty.")
    name = source_name(file.filename)
    logger.info("analysis started source=%s bytes=%s", name, len(raw))
    try:
        async with request.app.state.slots:
            document = await asyncio.to_thread(
                analyse_upload,
                name,
                redact_upload(text),
                settings,
            )
    except FormatError as exc:
        raise ApiError(422, "unsupported_log", str(exc)) from exc
    except Exception:
        logger.exception("analysis failed source=%s", name)
        raise ApiError(500, "analysis_failed", "The analysis failed.") from None
    incident = document.get("incident") if isinstance(document, dict) else None
    analysis = document.get("analysis") if isinstance(document, dict) else None
    logger.info(
        "analysis complete source=%s incident=%s enriched=%s",
        name,
        incident.get("id") if isinstance(incident, dict) else None,
        analysis.get("enriched") if isinstance(analysis, dict) else None,
    )
    return JSONResponse(document)
