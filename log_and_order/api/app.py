"""FastAPI application. Construction does not contact Ollama."""

import asyncio
import sys
from collections.abc import Callable

import httpx
from fastapi import FastAPI

from ..config import server_label
from .ollama import PROBE_TIMEOUT_SECONDS, OllamaStatus, check_llama, check_ollama, describe_target
from .routes import ApiError, handle_api_error, router
from .settings import ApiSettings, load_settings, model_origin


def default_client_factory(timeout: float) -> Callable[[], httpx.Client]:
    """A short-lived client for the readiness probe. Inference uses the chat client."""

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
    """Best-effort probe. A failure here does not stop the process."""
    label = server_label(settings.backend)
    timeout = min(PROBE_TIMEOUT_SECONDS, settings.ollama_timeout)
    try:
        if settings.backend == "llamacpp":
            return check_llama(
                settings.llama_base_url,
                timeout=timeout,
                api_key=settings.llama_api_key,
            )
        return check_ollama(settings.ollama_base_url, settings.model, timeout=timeout)
    except Exception as exc:
        print(
            f"{label} readiness check failed ({exc.__class__.__name__}). Starting anyway.",
            file=sys.stderr,
        )
        return OllamaStatus(False, "unreachable", [])


def announce(settings: ApiSettings, status: OllamaStatus) -> None:
    """Tell the operator whether enrichment can run. The API key is not printed."""
    target = describe_target(model_origin(settings))
    label = server_label(settings.backend)
    if status.ok:
        if settings.backend == "llamacpp":
            print(
                f"llama.cpp at {target} is available. The configured model label is {settings.model}.",
                file=sys.stderr,
            )
            return
        print(f"Ollama model {settings.model} is available at {target}.", file=sys.stderr)
        return
    if status.reason == "model_missing":
        print(
            f"Ollama at {target} does not have {settings.model}. "
            "The server will start, and enriched analysis stays unavailable until the model is installed.",
            file=sys.stderr,
        )
        return
    if status.reason == "loading":
        print(
            f"{label} at {target} is still loading its model. "
            "The server will start, and enriched analysis stays unavailable until the load finishes.",
            file=sys.stderr,
        )
        return
    print(
        f"{label} at {target} is not reachable. "
        "The server will start, and enriched analysis stays unavailable until it can be reached.",
        file=sys.stderr,
    )
