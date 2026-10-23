"""HTTP server settings. Read once at startup. The CLI does not use this module."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_UPLOAD_MB = 32
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_CONCURRENCY = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
MAX_UPLOAD_MB = 1024
MAX_CONCURRENCY = 32


class SettingsError(Exception):
    """Required HTTP configuration is missing or malformed."""


@dataclass(frozen=True)
class ApiSettings:
    """Connection and limit values for one server process."""

    ollama_base_url: str
    model: str
    api_key: str | None
    max_upload_bytes: int
    ollama_timeout: float
    max_concurrent: int
    host: str
    port: int
    backend: str = "ollama"
    llama_base_url: str = ""
    llama_api_key: str | None = None


def model_origin(settings: ApiSettings) -> str:
    """The origin the selected backend is probed and called on."""
    if settings.backend == "llamacpp":
        return settings.llama_base_url
    return settings.ollama_base_url


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def validate_origin(url: str, name: str, example: str) -> str:
    """Accept an http(s) origin. A path would let the process call a different route."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsError(
            f"{name} must be an http or https URL with a host, for example {example}."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SettingsError(f"{name} must not include a path, query, or fragment.")
    return url.strip().rstrip("/")


def validate_ollama_url(url: str) -> str:
    """Accept an Ollama origin."""
    return validate_origin(url, "OLLAMA_BASE_URL", "http://127.0.0.1:11434")


def load_settings(environ: Mapping[str, str] | None = None) -> ApiSettings:
    """Read the HTTP configuration. Raises SettingsError listing every problem."""
    env = os.environ if environ is None else environ
    errors: list[str] = []

    backend = _text(env, "LOG_AND_ORDER_BACKEND") or "ollama"
    if backend not in {"ollama", "llamacpp"}:
        errors.append("LOG_AND_ORDER_BACKEND must be ollama or llamacpp.")

    url = ""
    llama_url = ""
    if backend == "llamacpp":
        raw_llama = _text(env, "LLAMA_BASE_URL")
        if not raw_llama:
            errors.append("LLAMA_BASE_URL is required. Example: http://127.0.0.1:8080")
        else:
            try:
                llama_url = validate_origin(raw_llama, "LLAMA_BASE_URL", "http://127.0.0.1:8080")
            except SettingsError as exc:
                errors.append(str(exc))
    else:
        raw_url = _text(env, "OLLAMA_BASE_URL")
        if not raw_url:
            errors.append("OLLAMA_BASE_URL is required. Example: http://127.0.0.1:11434")
        else:
            try:
                url = validate_ollama_url(raw_url)
            except SettingsError as exc:
                errors.append(str(exc))

    model = _text(env, "LOG_AND_ORDER_MODEL")
    if not model:
        errors.append("LOG_AND_ORDER_MODEL is required. Example: LiquidAI/lfm2.5-2.6b")

    try:
        upload_mb = _positive_int(env, "LOG_AND_ORDER_MAX_UPLOAD_MB", DEFAULT_UPLOAD_MB)
    except SettingsError as exc:
        errors.append(str(exc))
        upload_mb = DEFAULT_UPLOAD_MB
    else:
        if upload_mb > MAX_UPLOAD_MB:
            errors.append(f"LOG_AND_ORDER_MAX_UPLOAD_MB must be at most {MAX_UPLOAD_MB}.")

    try:
        timeout = _positive_float(env, "LOG_AND_ORDER_OLLAMA_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    except SettingsError as exc:
        errors.append(str(exc))
        timeout = DEFAULT_TIMEOUT_SECONDS

    try:
        concurrent = _positive_int(env, "LOG_AND_ORDER_MAX_CONCURRENT_ANALYSES", DEFAULT_CONCURRENCY)
    except SettingsError as exc:
        errors.append(str(exc))
        concurrent = DEFAULT_CONCURRENCY
    else:
        if concurrent > MAX_CONCURRENCY:
            errors.append(f"LOG_AND_ORDER_MAX_CONCURRENT_ANALYSES must be at most {MAX_CONCURRENCY}.")

    host = _text(env, "LOG_AND_ORDER_HOST") or DEFAULT_HOST
    if any(character.isspace() for character in host) or "/" in host:
        errors.append("LOG_AND_ORDER_HOST must be a hostname or address without a path.")

    try:
        port = _positive_int(env, "LOG_AND_ORDER_PORT", DEFAULT_PORT)
    except SettingsError as exc:
        errors.append(str(exc))
        port = DEFAULT_PORT
    else:
        if port > 65535:
            errors.append("LOG_AND_ORDER_PORT must be from 1 to 65535.")

    if errors:
        raise SettingsError("\n".join(errors))

    api_key = _text(env, "LOG_AND_ORDER_API_KEY") or None
    llama_api_key = _text(env, "LLAMA_API_KEY") or None
    return ApiSettings(
        ollama_base_url=url,
        model=model,
        api_key=api_key,
        max_upload_bytes=upload_mb * 1024 * 1024,
        ollama_timeout=timeout,
        max_concurrent=concurrent,
        host=host,
        port=port,
        backend=backend,
        llama_base_url=llama_url,
        llama_api_key=llama_api_key,
    )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise SettingsError(f"{name} must be a positive integer.")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a positive number.") from exc
    if value <= 0:
        raise SettingsError(f"{name} must be a positive number.")
    return value
