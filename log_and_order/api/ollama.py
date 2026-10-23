"""Read-only checks against the configured model server.

The process never pulls a model. Ollama is checked with /api/tags.
llama.cpp is checked with /health, and /v1/models when a list is needed.
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


def llama_model_names(payload: object) -> list[str] | None:
    """Ids from a /v1/models body. None means the body is not that document."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    names: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            names.append(item["id"])
    return names


def _bearer(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def check_llama(
    base_url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    api_key: str | None = None,
    list_models: bool = False,
) -> OllamaStatus:
    """GET /health. A loaded model is ready even when its id differs from the label.

    /v1/models is read only for the model list. A 503 from /health means the
    server is still loading and is not a successful list.
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    headers = _bearer(api_key)
    origin = base_url.rstrip("/")
    try:
        try:
            health = http.get(origin + "/health", headers=headers)
        except httpx.HTTPError:
            return OllamaStatus(False, "unreachable", [])
        if health.status_code == 503:
            return OllamaStatus(False, "loading", [])
        if health.status_code != 200:
            return OllamaStatus(False, "unreachable", [])
        if not list_models:
            return OllamaStatus(True, "ok", [])
        try:
            listed = http.get(origin + "/v1/models", headers=headers)
        except httpx.HTTPError:
            return OllamaStatus(False, "unreachable", [])
        if listed.status_code != 200:
            return OllamaStatus(False, "unreachable", [])
        try:
            names = llama_model_names(listed.json())
        except ValueError:
            names = None
        if names is None:
            return OllamaStatus(False, "unreachable", [])
        return OllamaStatus(True, "ok", names)
    finally:
        if owns_client:
            http.close()
