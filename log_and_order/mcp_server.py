"""MCP server. A thin stdio adapter over the existing incident pipeline.

The server is read-only. It does not import the command-line interface.
`create_server` is transport-agnostic so a later HTTP app can reuse it.
"""

import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Literal

from .config import (
    MCP_DEFAULT_LINE_COUNT,
    MCP_MAX_INPUT_BYTES,
    MCP_MAX_LINE_CHARS,
    MCP_MAX_LINE_COUNT,
)
from .files import read_text_file
from .logs.jenkins.text import redact
from .pipeline import FormatError, incident_payload

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - the extra is installed for development
    MCPServer = None  # type: ignore[misc, assignment]
    ToolError = None  # type: ignore[misc, assignment]

ROOT_ENV = "LOG_AND_ORDER_ROOT"
SUBMITTED_SOURCE = "submitted-text"
INSTRUCTIONS = (
    "Read-only log analysis for coding agents. This server does not edit code, "
    "run commands from logs, or connect to a Jenkins server. "
    "analyse_log and analyse_text return Log and Order's deterministic JSON incident report. "
    "use_llm defaults to false. Set it true only for optional local-model enrichment; "
    "if that model is unavailable the factual report is still returned. "
    "log_line and evidence line numbers are lines in the log. source_line is a line in application code. "
    "Call read_log_lines to fetch the original log text for a cited log line. "
    "detail=compact omits log excerpts; detail=full returns the complete incident document. "
    "A saved Jenkins console log can be analysed without a running Jenkins server."
)


class RootError(Exception):
    """LOG_AND_ORDER_ROOT is set but cannot be used."""


def configured_root(*, announce: bool = False) -> Path:
    """The only directory file tools may read.

    An unset variable means the process working directory. A set variable must
    already be a directory. Symlinks in that path are resolved.
    """
    raw = os.environ.get(ROOT_ENV, "").strip()
    if not raw:
        if announce:
            print(
                "LOG_AND_ORDER_ROOT is unset. File tools are limited to the current working directory.",
                file=sys.stderr,
            )
        try:
            return Path.cwd().resolve()
        except OSError as exc:
            raise RootError("The current working directory is not readable.") from exc
    try:
        resolved = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise RootError("LOG_AND_ORDER_ROOT is not a readable directory.") from exc
    if not resolved.is_dir():
        raise RootError("LOG_AND_ORDER_ROOT is not a readable directory.")
    return resolved


def _tool_error(message: str) -> ToolError:
    if ToolError is None:
        raise RuntimeError(message)
    return ToolError(message)


def _root() -> Path:
    try:
        return configured_root()
    except RootError as exc:
        raise _tool_error(str(exc)) from exc


def resolve_log_path(path: str) -> Path:
    """Resolve a tool path and keep it inside the allowed root."""
    if not path or "\0" in path:
        raise _tool_error("Not a file.")
    root = _root()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise _tool_error("Not a file.") from exc
    if not resolved.is_relative_to(root):
        raise _tool_error("Path is outside the allowed root.")
    try:
        info = resolved.stat()
    except OSError as exc:
        raise _tool_error("Not a file.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _tool_error("Not a file.")
    if info.st_size > MCP_MAX_INPUT_BYTES:
        raise _tool_error("The file is larger than the configured limit.")
    return resolved


def read_bounded(path: Path) -> str:
    """Read a regular file that has already been checked against the root."""
    try:
        text = read_text_file(path)
    except ValueError as exc:
        raise _tool_error("The file looks like a binary file.") from exc
    except OSError as exc:
        raise _tool_error("Could not read the file.") from exc
    if len(text.encode("utf-8", errors="replace")) > MCP_MAX_INPUT_BYTES:
        raise _tool_error("The file is larger than the configured limit.")
    return text


def redact_lines(text: str) -> str:
    """Redact each line in place so later line numbers stay put."""
    if not text:
        return text
    newline = text.endswith("\n")
    redacted = "\n".join(redact(line) for line in text.splitlines())
    if newline:
        redacted += "\n"
    return redacted


def _clip(text: str) -> str:
    if len(text) <= MCP_MAX_LINE_CHARS:
        return text
    return text[: MCP_MAX_LINE_CHARS - 15].rstrip() + "...[truncated]"


def _model_name(model: str | None) -> str | None:
    if model is None:
        return None
    cleaned = model.strip()
    return cleaned or None


def _check_detail(detail: str) -> None:
    if detail not in {"full", "compact"}:
        raise _tool_error("detail must be full or compact.")


def compact_report(document: dict[str, Any]) -> dict[str, Any]:
    """Incident facts and evidence references, without log excerpts."""
    payload: dict[str, Any] = {
        "schema_version": document["schema_version"],
        "detail": "compact",
        "source": document["source"],
        "incident": document["incident"],
        "locations": document["locations"],
        "timeline": [
            {
                "timestamp": moment["timestamp"],
                "log_line": moment["log_line"],
                "kind": moment["kind"],
            }
            for moment in document["timeline"]
        ],
        "evidence_lines": [
            {
                "number": line["number"],
                "correlation": line["correlation"],
                "role": line["role"],
            }
            for line in document["evidence"]["lines"]
        ],
        "unknowns": document["evidence"]["unknowns"],
        "analysis": document["analysis"],
        "other_warnings": document["other_warnings"],
    }
    if "jenkins" in document:
        payload["jenkins"] = document["jenkins"]
    return payload


def _present(document: dict[str, Any], detail: str) -> dict[str, Any]:
    if detail == "compact":
        return compact_report(document)
    return document


def analyse_content(
    path: Path,
    text: str,
    *,
    use_llm: bool,
    model: str | None,
    detail: str,
    empty_message: str,
) -> dict[str, Any]:
    """Run the existing JSON pipeline on text that is already in memory."""
    _check_detail(detail)
    if "\0" in text:
        raise _tool_error("The text looks like a binary file.")
    if len(text.encode("utf-8", errors="replace")) > MCP_MAX_INPUT_BYTES:
        raise _tool_error("The text is larger than the configured limit.")
    redacted = redact_lines(text)
    if not redacted.strip():
        raise _tool_error(empty_message)
    try:
        document = incident_payload(
            path,
            redacted,
            use_llm=use_llm,
            model=_model_name(model),
        )
    except FormatError as exc:
        raise _tool_error(str(exc)) from exc
    return _present(document, detail)


def create_server() -> MCPServer:
    """Build the server. The caller chooses the transport."""
    if MCPServer is None:
        raise ImportError("mcp is not installed")
    mcp = MCPServer(
        "log-and-order",
        instructions=INSTRUCTIONS,
        log_level="WARNING",
    )

    @mcp.tool()
    def analyse_log(
        path: str,
        use_llm: bool = False,
        model: str | None = None,
        detail: Literal["full", "compact"] = "full",
    ) -> dict[str, Any]:
        """Analyse a log file and return Log and Order's JSON incident report.

        path is a file inside LOG_AND_ORDER_ROOT. use_llm defaults to false and
        keeps the report deterministic. Set use_llm true to add optional local
        model enrichment; if that model is unavailable the factual report is
        still returned. model overrides the configured Ollama model.
        detail=full is the existing JSON incident document. detail=compact keeps
        the incident, locations, and evidence line numbers without log excerpts.
        source_line is a line in application code. Evidence numbers are log lines;
        fetch those with read_log_lines. This tool does not edit files or connect
        to Jenkins.
        """
        resolved = resolve_log_path(path)
        return analyse_content(
            resolved,
            read_bounded(resolved),
            use_llm=use_llm,
            model=model,
            detail=detail,
            empty_message="The file is empty.",
        )

    @mcp.tool()
    def read_log_lines(
        path: str,
        start_line: int,
        count: int = MCP_DEFAULT_LINE_COUNT,
    ) -> dict[str, Any]:
        """Read a bounded range of original log lines, with their line numbers.

        Line numbers match the incident report. The text is redacted. count is
        capped at the server maximum. This tool does not interpret the lines
        and does not modify the file.
        """
        if start_line < 1:
            raise _tool_error("start_line must be 1 or greater.")
        if count < 1:
            raise _tool_error("count must be 1 or greater.")
        text = read_bounded(resolve_log_path(path))
        if not text.strip():
            raise _tool_error("The file is empty.")
        lines = text.splitlines()
        if start_line > len(lines):
            raise _tool_error("start_line is past the end of the file.")
        if count > MCP_MAX_LINE_COUNT:
            count = MCP_MAX_LINE_COUNT
        end = min(start_line - 1 + count, len(lines))
        selected = [
            {"number": number, "text": _clip(redact(lines[number - 1]))}
            for number in range(start_line, end + 1)
        ]
        return {"path": path, "total_lines": len(lines), "lines": selected}

    @mcp.tool()
    def analyse_text(
        content: str,
        use_llm: bool = False,
        model: str | None = None,
        detail: Literal["full", "compact"] = "full",
    ) -> dict[str, Any]:
        """Analyse log text that is not stored in a file.

        Line numbers are relative to content. The result uses the same JSON
        incident schema as analyse_log. use_llm defaults to false. Nothing is
        written to disk.
        """
        return analyse_content(
            Path(SUBMITTED_SOURCE),
            content,
            use_llm=use_llm,
            model=model,
            detail=detail,
            empty_message="The text is empty.",
        )

    return mcp


def main() -> int:
    """Serve over stdio. Progress and errors stay on stderr."""
    if MCPServer is None:
        print(
            "The mcp extra is not installed. Run: uv sync --extra mcp",
            file=sys.stderr,
        )
        return 1
    try:
        configured_root(announce=True)
    except RootError as exc:
        print(exc, file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    create_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
