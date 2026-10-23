"""Model calls. Importing this module does not connect to a server."""
import re
import sys

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from ollama import ResponseError
from openai import APIConnectionError, APIStatusError

from ..config import (
    DEFAULT_LLAMA_URL,
    MODEL_NAME,
)

THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
THINK_OPEN = re.compile(r"<think\b", re.IGNORECASE)

# The model server did not return a reply. A timeout is an APIConnectionError.
# Enrichment does not retry these, and the factual report is still written.
MODEL_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    ResponseError,
    APIConnectionError,
    APIStatusError,
    ConnectionError,
)


class GenerationError(Exception):
    """The model finished without producing an answer."""


def message_text(message: BaseMessage) -> str:
    """Flatten a LangChain message that is either a string or content blocks.

    A llama.cpp thinking trace split into reasoning_content is not part of
    content, so it stays out of the answer. A raw think block is removed later.
    """
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def strip_think(text: str) -> str | None:
    """Return the answer outside any thinking trace.

    A reply that never leaves an unclosed think block is not an answer.
    """
    if THINK_OPEN.search(text) and "</think>" not in text.lower():
        before = THINK_OPEN.split(text, maxsplit=1)[0].strip()
        return before or None
    cleaned = THINK_BLOCK.sub("", text)
    cleaned = re.sub(r"</?think\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    return cleaned or None

def collect_summary(chain, payload: dict[str, str]) -> str | None:
    """Stream one reply and drop a thinking trace. None means there was no answer."""
    pieces: list[str] = []
    for chunk in chain.stream(payload):
        text = message_text(chunk)
        if text:
            pieces.append(text)
    return strip_think("".join(pieces))


def complete(chain, payload: dict[str, str]) -> str:
    """Run the chain, retrying once when the model only emits a think block."""
    for attempt in range(2):
        answer = collect_summary(chain, payload)
        if answer:
            return answer
        if attempt == 0:
            print(
                "The model returned only a thinking trace. Retrying...",
                file=sys.stderr,
            )
    raise GenerationError("The model did not produce a summary.")


def llama_openai_url(origin: str | None) -> str:
    """OpenAI-compatible base URL for a llama.cpp origin."""
    base = (origin or DEFAULT_LLAMA_URL).strip().rstrip("/")
    return f"{base}/v1"


def build_llm(
    *,
    reasoning: bool = False,
    num_predict: int = 1024,
    model: str | None = None,
    json_mode: bool = False,
    base_url: str | None = None,
    timeout: float | None = None,
    backend: str = "ollama",
    api_key: str | None = None,
) -> BaseChatModel:
    """Build the local chat model.

    The human log report turns reasoning on so a thinking trace stays out of
    the printed answer. Structured enrichment leaves reasoning off and asks
    for JSON. base_url and timeout are omitted for Ollama when unset, so that
    client keeps its own default host and no deadline. llama.cpp uses
    {origin}/v1, and an omitted origin is the local llama-server port.
    This function does not read the environment. Callers pass the backend,
    base_url, timeout, and API key.
    """
    if backend == "ollama":
        return _build_ollama(
            reasoning=reasoning,
            num_predict=num_predict,
            model=model,
            json_mode=json_mode,
            base_url=base_url,
            timeout=timeout,
        )
    if backend == "llamacpp":
        return _build_llama(
            reasoning=reasoning,
            num_predict=num_predict,
            model=model,
            json_mode=json_mode,
            base_url=base_url,
            timeout=timeout,
            api_key=api_key,
        )
    raise ValueError("Model backend must be ollama or llamacpp.")


def _build_ollama(
    *,
    reasoning: bool,
    num_predict: int,
    model: str | None,
    json_mode: bool,
    base_url: str | None,
    timeout: float | None,
) -> ChatOllama:
    options: dict = {}
    if base_url:
        options["base_url"] = base_url
    if timeout is not None:
        options["client_kwargs"] = {"timeout": timeout}
    return ChatOllama(
        model=model or MODEL_NAME,
        temperature=0.1,
        reasoning=reasoning,
        num_ctx=8192,
        num_predict=num_predict,
        format="json" if json_mode else None,
        **options,
    )


def _build_llama(
    *,
    reasoning: bool,
    num_predict: int,
    model: str | None,
    json_mode: bool,
    base_url: str | None,
    timeout: float | None,
    api_key: str | None,
) -> ChatOpenAI:
    """One chat client for PrismML's server and mainstream llama-server.

    Context size is the server's -c and is not sent. max_tokens is the cap
    both servers document. max_completion_tokens is the same cap under the
    name the OpenAI client sends. Thinking stays available for the human
    report, and the server is asked to keep that trace out of the answer.
    Enrichment and ordinary summaries turn thinking off. The SDK must not
    retry a connection failure; this program decides when a reply is retried.
    """
    extra: dict = {"max_tokens": num_predict}
    options: dict = {}
    if reasoning:
        extra["reasoning_format"] = "auto"
    else:
        extra["chat_template_kwargs"] = {"enable_thinking": False}
        options["reasoning_effort"] = "none"
    if json_mode:
        options["model_kwargs"] = {"response_format": {"type": "json_object"}}
    if timeout is not None:
        options["timeout"] = timeout
    return ChatOpenAI(
        model=model or MODEL_NAME,
        temperature=0.1,
        max_completion_tokens=num_predict,
        base_url=llama_openai_url(base_url),
        api_key=api_key or "not-needed",
        max_retries=0,
        use_responses_api=False,
        extra_body=extra,
        **options,
    )
