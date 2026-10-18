"""FastAPI application. Construction does not contact Ollama."""

import asyncio
import sys
from collections.abc import Callable

import httpx
from fastapi import FastAPI

from .ollama import PROBE_TIMEOUT_SECONDS, OllamaStatus, check_ollama, describe_target
from .routes import ApiError, handle_api_error, router
from .settings import ApiSettings, load_settings


def default_client_factory(timeout: float) -> Callable[[], httpx.Client]:
    """A short-lived client for /api/tags. Inference uses the Ollama chat client."""

    def make() -> httpx.Client:
        return httpx.Client(timeout=min(PROBE_TIMEOUT_SECONDS, timeout))

    return make


def create_app(
    settings: ApiSettings | None = None,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> FastAPI:
    """Build the application. Pass settings in tests; otherwise read the environment."""
    resolved = settings if settings is not None else load_settings()
    app = FastAPI(
        title="Log and Order",
        summary="HTTP interface to the existing incident pipeline.",
    )
    app.state.settings = resolved
    app.state.slots = asyncio.Semaphore(resolved.max_concurrent)
    app.state.client_factory = client_factory or default_client_factory(resolved.ollama_timeout)
    app.add_exception_handler(ApiError, handle_api_error)
    app.include_router(router, prefix="/api/v1")
    return app


def startup_status(settings: ApiSettings) -> OllamaStatus:
    """Best-effort tags check. A failure here does not stop the process."""
    try:
        return check_ollama(
            settings.ollama_base_url,
            settings.model,
            timeout=min(PROBE_TIMEOUT_SECONDS, settings.ollama_timeout),
        )
    except Exception as exc:
        print(
            f"Ollama readiness check failed ({exc.__class__.__name__}). Starting anyway.",
            file=sys.stderr,
        )
        return OllamaStatus(False, "unreachable", [])


def announce(settings: ApiSettings, status: OllamaStatus) -> None:
    """Tell the operator whether enrichment can run. The API key is not printed."""
    target = describe_target(settings.ollama_base_url)
    if status.ok:
        print(f"Ollama model {settings.model} is available at {target}.", file=sys.stderr)
        return
    if status.reason == "model_missing":
        print(
            f"Ollama at {target} does not have {settings.model}. "
            "The server will start, and enriched analysis stays unavailable until the model is installed.",
            file=sys.stderr,
        )
        return
    print(
        f"Ollama at {target} is not reachable. "
        "The server will start, and enriched analysis stays unavailable until it can be reached.",
        file=sys.stderr,
    )
