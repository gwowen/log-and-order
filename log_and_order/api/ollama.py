"""Read-only checks against the configured Ollama server.

The process never pulls a model and never calls any route except /api/tags.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class OllamaStatus:
    """Result of one tags request. models is empty when the server did not answer."""

    ok: bool
    reason: str
    models: list[str]


def describe_target(url: str) -> str:
    """Scheme, host, and port. Userinfo is left out so logs do not keep credentials."""
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown"
    if parsed.port:
        return f"{parsed.scheme}://{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


def model_available(names: list[str], configured: str) -> bool:
    """True when the configured name is listed, including an Ollama tag suffix."""
    if configured in names:
        return True
    prefix = configured + ":"
    return any(name.startswith(prefix) for name in names)


def model_names(payload: object) -> list[str] | None:
    """Names from an /api/tags body. None means the body is not that document."""
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    names: list[str] = []
    for item in models:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def check_ollama(
    base_url: str,
    model: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> OllamaStatus:
    """GET /api/tags and see whether the configured model is installed."""
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = http.get(base_url.rstrip("/") + "/api/tags")
        except httpx.HTTPError:
            return OllamaStatus(False, "unreachable", [])
        if response.status_code != 200:
            return OllamaStatus(False, "unreachable", [])
        try:
            names = model_names(response.json())
        except ValueError:
            names = None
        if names is None:
            return OllamaStatus(False, "unreachable", [])
        if not model_available(names, model):
            return OllamaStatus(False, "model_missing", names)
        return OllamaStatus(True, "ok", names)
    finally:
        if owns_client:
            http.close()
