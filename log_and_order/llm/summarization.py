"""Summarize an ordinary text file, or narrate one extracted incident."""

import sys
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from ..config import MODEL_NAME
from ..logs.selection import event_id
from ..models import Event
from ..reports.human import format_model_input
from .client import (
    GenerationError,
    build_llm,
    collect_summary,
    complete,
)
from .prompts import REPORT_PROMPT


def is_none(text: str) -> bool:
    """True when a chunk summary has nothing worth combining."""
    return text.strip().upper() in {"NONE", "NONE."}


# Ollama answers fit in the smaller cap. llama.cpp reasoning models, including
# Bonsai, can spend several thousand tokens inside the trace before the answer.
OLLAMA_NARRATIVE_BUDGETS = (2048, 4096)
LLAMA_NARRATIVE_BUDGETS = (8192, 16384)


def narrative_budgets(backend: str) -> tuple[int, int]:
    """Token caps for the human report. The second value is the one retry."""
    if backend == "llamacpp":
        return LLAMA_NARRATIVE_BUDGETS
    return OLLAMA_NARRATIVE_BUDGETS


def summarize_chunks(
    path: Path,
    chunks: list[str],
    *,
    piece_prompt: ChatPromptTemplate,
    combine_prompt: ChatPromptTemplate,
    llm: BaseChatModel,
) -> str:
    """Summarize each chunk, drop NONE parts, and combine what remains."""
    piece_chain = piece_prompt | llm
    if len(chunks) == 1:
        return complete(piece_chain, {"source": path.name, "text": chunks[0]})

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"Summarizing part {index} of {len(chunks)}...",
            file=sys.stderr,
        )
        partial = complete(piece_chain, {"source": path.name, "text": chunk})
        if not is_none(partial):
            partials.append(partial)

    if not partials:
        return "NONE"
    if len(partials) == 1:
        return partials[0]

    print("Combining partial summaries...", file=sys.stderr)
    combine_chain = combine_prompt | llm
    return complete(
        combine_chain,
        {"source": path.name, "text": "\n\n".join(partials)},
    )


def interpret_events(
    path: Path,
    event: Event,
    model: str | None = None,
    *,
    prompt: ChatPromptTemplate | None = None,
    incident_text: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    backend: str = "ollama",
    api_key: str | None = None,
) -> str:
    """Narrate one incident. An empty thinking trace is retried once with a larger budget.

    Ollama retries from 2048 tokens to 4096. llama.cpp retries from 8192 to
    16384, because a thinking model can use the smaller cap before it answers.
    The same budget is not tried twice. A second empty reply raises GenerationError.
    Jenkins reports pass their own prompt and the extracted incident text.
    """
    chosen = model or MODEL_NAME
    print(
        f"Interpreting event {event_id(event)} with {chosen}...",
        file=sys.stderr,
    )
    payload = {
        "source": path.name,
        "text": incident_text if incident_text is not None else format_model_input(event),
    }
    budgets = narrative_budgets(backend)
    for attempt, budget in enumerate(budgets):
        if attempt:
            print(
                "The model returned only a thinking trace. "
                "Retrying with a larger token budget...",
                file=sys.stderr,
            )
        chain = (prompt or REPORT_PROMPT) | build_llm(
            reasoning=True,
            num_predict=budget,
            model=model,
            base_url=base_url,
            timeout=timeout,
            backend=backend,
            api_key=api_key,
        )
        answer = collect_summary(chain, payload)
        if answer:
            return answer
    raise GenerationError("The model did not produce a summary.")
