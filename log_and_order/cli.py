"""Command line. The report goes to stdout and progress goes to stderr."""

import argparse
import sys
from pathlib import Path

import httpx
from ollama import ResponseError
from openai import APIConnectionError, APIStatusError, APITimeoutError

from .config import (
    MODEL_NAME,
    configured_backend,
    configured_base_url,
    configured_llama_api_key,
    configured_llama_url,
    configured_model,
    server_label,
)
from .files import read_text_file as read_file
from .llm.client import GenerationError
from .logs.extraction import extract_events
from .logs.jenkins.analysis import analyse_jenkins
from .pipeline import FormatError, summarize
from .reports.human import dump_events
from .reports.jenkins import dump_jenkins


def publish(text: str, output: Path | None) -> None:
    """Print the report, or write it when --output is set. Stdout stays empty then."""
    if not text.endswith("\n"):
        text += "\n"
    if output is None:
        print(text, end="")
        return
    output.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Command line. The human report stays the default."""
    parser = argparse.ArgumentParser(
        description="Order a log into an incident report, or summarize another text file.",
    )
    parser.add_argument("file", type=Path, help="Path to the file to summarize")
    parser.add_argument(
        "--format",
        dest="report_format",
        choices=("human", "agent", "json"),
        default="human",
        help="human is the narrative report. agent is a Markdown brief. json is the same incident as data.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Do not call the model. Agent and JSON reports stay deterministic. The human log report uses the extracted lines.",
    )
    parser.add_argument(
        "--model",
        help=(
            "Model name. Defaults to LOG_AND_ORDER_MODEL when set, "
            f"otherwise {MODEL_NAME}. llama.cpp accepts any label, for example bonsai."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("ollama", "llamacpp"),
        help="Model server. Defaults to LOG_AND_ORDER_BACKEND when set, otherwise ollama.",
    )
    parser.add_argument(
        "--ollama-url",
        help="Ollama server URL. Defaults to OLLAMA_BASE_URL when set, otherwise the local Ollama client.",
    )
    parser.add_argument(
        "--llama-url",
        help=(
            "llama.cpp origin, without a path. Defaults to LLAMA_BASE_URL when set, "
            "otherwise http://127.0.0.1:8080. Used when the backend is llamacpp."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the report to this file instead of stdout.",
    )
    parser.add_argument(
        "--dump-events",
        action="store_true",
        help="Print the extracted incident groups and exit without calling the model.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read one file and print its summary. Returns a process status code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    path: Path = args.file
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    try:
        text = read_file(path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not read {path}: {exc}", file=sys.stderr)
        return 1

    if not text.strip():
        print("The file is empty.")
        return 0

    if args.dump_events:
        # Dump ignores --format. It shows the human-report model input.
        jenkins = analyse_jenkins(text)
        if jenkins is not None:
            try:
                publish(dump_jenkins(jenkins), args.output)
            except OSError as exc:
                print(f"Could not write {args.output}: {exc}", file=sys.stderr)
                return 1
            return 0
        events = extract_events(text)
        if events is None:
            publish("Not a timestamped log.", args.output)
            return 0
        try:
            publish(dump_events(events), args.output)
        except OSError as exc:
            print(f"Could not write {args.output}: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        backend = configured_backend(args.backend)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if backend == "llamacpp":
        base_url = configured_llama_url(args.llama_url)
        api_key = configured_llama_api_key()
    else:
        base_url = configured_base_url(args.ollama_url)
        api_key = None
    server = server_label(backend)

    try:
        summary = summarize(
            path,
            text,
            args.report_format,
            use_llm=not args.no_llm,
            model=configured_model(args.model),
            base_url=base_url,
            backend=backend,
            api_key=api_key,
        )
    except FormatError as exc:
        print(exc, file=sys.stderr)
        return 1
    except APITimeoutError as exc:
        print(f"{server} request failed: {exc}", file=sys.stderr)
        return 1
    except (httpx.ConnectError, ConnectionError, APIConnectionError):
        print(
            f"Could not connect to {server}. Start it and try again.",
            file=sys.stderr,
        )
        return 1
    except (ResponseError, APIStatusError) as exc:
        print(f"{server} request failed: {exc}", file=sys.stderr)
        return 1
    except GenerationError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        publish(summary, args.output)
    except OSError as exc:
        print(f"Could not write {args.output}: {exc}", file=sys.stderr)
        return 1
    return 0
