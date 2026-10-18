"""Ollama calls. Importing this module does not connect to Ollama."""
import re
import sys

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from ..config import (
    MODEL_NAME,
)

THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
THINK_OPEN = re.compile(r"<think\b", re.IGNORECASE)


class GenerationError(Exception):
    """The model finished without producing an answer."""


def message_text(message: BaseMessage) -> str:
    """Flatten a LangChain message that is either a string or content blocks."""
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


def llm_base_url(llm: ChatOllama) -> str | None:
    """The Ollama URL stored on a client, if one was supplied."""
    value = getattr(llm, "base_url", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def llm_timeout(llm: ChatOllama) -> float | None:
    """The request timeout stored on a client, if one was supplied."""
    kwargs = getattr(llm, "client_kwargs", None) or {}
    timeout = kwargs.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return None
    return float(timeout)


def build_llm(
    *,
    reasoning: bool = False,
    num_predict: int = 1024,
    model: str | None = None,
    json_mode: bool = False,
    base_url: str | None = None,
    timeout: float | None = None,
) -> ChatOllama:
    """Build the local chat model.

    The human log report turns reasoning on so a thinking trace stays out of
    the printed answer. Structured enrichment leaves reasoning off and asks
    Ollama for JSON. base_url and timeout are omitted when unset, so the
    Ollama client keeps its own default host and no deadline.
    """
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
