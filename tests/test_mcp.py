"""MCP server tools, filesystem limits, and one stdio session."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from log_and_order.mcp_server import (
    ROOT_ENV,
    configured_root,
    create_server,
    main,
)
from log_and_order.pipeline import summarize
from tests.test_enrichment import FakeModel, PromptModel, patch_llm

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "example-logs"
SPRING = "example-logs/spring_boot_incident_noise.log"
JENKINS = "example-logs/jenkins_typescript_failure.log"
SHELL = "example-logs/jenkins_shell_failure.log"


def secret_log() -> str:
    """Five timestamped records plus a stack. The error line carries two credentials."""
    lines = [
        "2024-01-01T00:00:01Z INFO app : started",
        "2024-01-01T00:00:02Z INFO app : ready",
        "2024-01-01T00:00:03Z INFO app : listening",
        "2024-01-01T00:00:04Z INFO app : serving",
        "2024-01-01T00:00:05Z ERROR app : password=supersecret ghp_abcdefghijklmnopqrstuvwxyz failed",
        "java.lang.IllegalStateException: boom",
        "\tat com.example.App.handle(App.java:12)",
    ]
    return "\n".join(lines) + "\n"


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    def payload(self, result) -> dict:
        self.assertFalse(result.is_error, getattr(result.content[0], "text", result))
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    def error_text(self, result) -> str:
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        return result.content[0].text

    async def test_lists_tools_and_names_itself(self) -> None:
        async with Client(create_server(), raise_exceptions=True) as client:
            self.assertIsNotNone(client.server_info)
            self.assertEqual(client.server_info.name, "log-and-order")
            self.assertIn("does not edit code", client.instructions or "")
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(tools), {"analyse_log", "read_log_lines", "analyse_text"})
        analyse = tools["analyse_log"].input_schema
        self.assertIn("path", analyse["required"])
        self.assertEqual(analyse["properties"]["use_llm"]["default"], False)
        self.assertEqual(analyse["properties"]["detail"]["default"], "full")
        self.assertEqual(tools["read_log_lines"].input_schema["properties"]["count"]["default"], 30)
        self.assertIn("content", tools["analyse_text"].input_schema["required"])

    async def test_repeated_calls_analyse_the_spring_log(self) -> None:
        text = (LOGS / "spring_boot_incident_noise.log").read_text()
        with redirect_stderr(io.StringIO()):
            expected = json.loads(
                summarize(
                    LOGS / "spring_boot_incident_noise.log",
                    text,
                    "json",
                    use_llm=False,
                )
            )
        with (
            patch.dict(os.environ, {ROOT_ENV: str(ROOT)}),
            patch("log_and_order.pipeline.build_llm", side_effect=AssertionError("llm")),
        ):
            async with Client(create_server(), raise_exceptions=True) as client:
                first = await client.call_tool("analyse_log", {"path": SPRING})
                second = await client.call_tool("analyse_log", {"path": SPRING, "use_llm": False})
                failure = next(
                    line
                    for line in self.payload(first)["evidence"]["lines"]
                    if line["role"] == "failure"
                )
                read = await client.call_tool(
                    "read_log_lines",
                    {"path": SPRING, "start_line": failure["number"], "count": 1},
                )
        report = self.payload(first)
        self.assertEqual(report, self.payload(second))
        self.assertEqual(report, expected)
        self.assertEqual(report["schema_version"], 1)
        self.assertNotIn("detail", report)
        self.assertEqual(report["incident"]["exception"], "java.lang.NoSuchMethodError")
        self.assertEqual(report["incident"]["service"], "orders-api")
        self.assertEqual(report["analysis"]["reason"], "disabled")
        self.assertFalse(report["analysis"]["enriched"])
        self.assertEqual(report["analysis"]["hypotheses"], [])
        self.assertEqual(report["evidence"]["possible_explanations"], [])
        location = report["locations"][0]
        self.assertEqual(location["source_line"], 87)
        self.assertEqual(location["log_line"], 649)
        self.assertNotEqual(location["source_line"], failure["number"])
        fetched = self.payload(read)
        self.assertEqual(fetched["lines"], [{"number": failure["number"], "text": failure["text"]}])
        self.assertGreater(fetched["total_lines"], failure["number"])

    async def test_analyses_a_jenkins_console(self) -> None:
        with patch.dict(os.environ, {ROOT_ENV: str(ROOT)}):
            async with Client(create_server(), raise_exceptions=True) as client:
                result = await client.call_tool(
                    "analyse_log",
                    {"path": JENKINS, "use_llm": False, "detail": "full"},
                )
                report = self.payload(result)
                log_line = report["locations"][0]["log_line"]
                read = await client.call_tool(
                    "read_log_lines",
                    {"path": JENKINS, "start_line": log_line, "count": 1},
                )
                compact = await client.call_tool(
                    "analyse_log",
                    {"path": JENKINS, "detail": "compact"},
                )
        jenkins = report["jenkins"]
        self.assertEqual(jenkins["log_type"], "jenkins")
        self.assertEqual(jenkins["build_result"], "FAILURE")
        self.assertEqual(jenkins["failed_stage"], "Build")
        self.assertEqual(jenkins["failure_category"], "BUILD")
        self.assertEqual(jenkins["failed_command"], "npm run build")
        self.assertEqual(jenkins["exit_code"], 2)
        primary = jenkins["primary_failure"]
        self.assertEqual(primary["source_file"], "src/server.ts")
        self.assertEqual(primary["source_line"], 42)
        self.assertIn(log_line, primary["log_lines"])
        self.assertNotEqual(primary["source_line"], log_line)
        self.assertEqual(report["evidence"]["possible_explanations"], [])
        fetched = self.payload(read)
        self.assertEqual(fetched["lines"][0]["number"], log_line)
        self.assertIn("src/server.ts", fetched["lines"][0]["text"])
        self.assertIn("TS2322", fetched["lines"][0]["text"])
        brief = self.payload(compact)
        self.assertEqual(brief["detail"], "compact")
        self.assertNotIn("evidence", brief)
        self.assertEqual(brief["jenkins"]["primary_failure"]["source_file"], "src/server.ts")
        self.assertEqual(
            [line["number"] for line in brief["evidence_lines"]],
            [line["number"] for line in report["evidence"]["lines"]],
        )
        for line in brief["evidence_lines"]:
            self.assertEqual(set(line), {"number", "correlation", "role"})
        for moment in brief["timeline"]:
            self.assertEqual(set(moment), {"timestamp", "log_line", "kind"})
        self.assertNotIn("TS2322", json.dumps(brief["timeline"]))

    async def test_optional_enrichment_uses_the_existing_validator(self) -> None:
        seen: list = []

        def factory(*args, **kwargs):
            seen.append(kwargs.get("model"))
            return PromptModel()

        with (
            patch.dict(os.environ, {ROOT_ENV: str(ROOT)}),
            patch_llm(side_effect=factory),
            redirect_stderr(io.StringIO()),
        ):
            async with Client(create_server(), raise_exceptions=True) as client:
                blank = await client.call_tool(
                    "analyse_log",
                    {"path": SPRING, "use_llm": True, "model": "  "},
                )
                chosen = await client.call_tool(
                    "analyse_log",
                    {"path": SPRING, "use_llm": True, "model": "custom-model"},
                )
        self.assertEqual(seen, [None, "custom-model"])
        report = self.payload(chosen)
        self.assertTrue(report["analysis"]["enriched"])
        self.assertFalse(report["analysis"]["fallback"])
        self.assertEqual(
            report["analysis"]["failure_mechanism"],
            "The logged call failed with the recorded exception.",
        )
        self.assertTrue(report["analysis"]["hypotheses"])
        self.assertNotIn("/tmp/not-in-the-log.java", json.dumps(report["analysis"]))
        self.assertEqual(self.payload(blank)["analysis"]["enriched"], True)

    async def test_enrichment_falls_back_when_the_model_is_down(self) -> None:
        fake = FakeModel([httpx.ConnectError("refused")])
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {ROOT_ENV: str(ROOT)}),
            patch_llm(return_value=fake),
            redirect_stderr(stderr),
        ):
            async with Client(create_server(), raise_exceptions=True) as client:
                result = await client.call_tool(
                    "analyse_log",
                    {"path": SPRING, "use_llm": True},
                )
        report = self.payload(result)
        self.assertEqual(report["incident"]["exception"], "java.lang.NoSuchMethodError")
        self.assertEqual(report["analysis"]["reason"], "unavailable")
        self.assertIsNone(report["analysis"]["failure_mechanism"])
        self.assertEqual(report["analysis"]["hypotheses"], [])
        self.assertIn("Enrichment failed", stderr.getvalue())

    async def test_analyse_text_keeps_submitted_line_numbers_and_redacts(self) -> None:
        content = secret_log()
        async with Client(create_server(), raise_exceptions=True) as client:
            result = await client.call_tool("analyse_text", {"content": content})
            empty = await client.call_tool("analyse_text", {"content": " \n"})
            notes = await client.call_tool(
                "analyse_text",
                {"content": "This is a meeting note.\nIt has no timestamps.\n"},
            )
            binary = await client.call_tool("analyse_text", {"content": "log\0text"})
        report = self.payload(result)
        rendered = json.dumps(report)
        self.assertEqual(report["source"], "submitted-text")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["analysis"]["reason"], "disabled")
        self.assertNotIn("supersecret", rendered)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", rendered)
        self.assertIn("[REDACTED]", rendered)
        cited = [line["number"] for line in report["evidence"]["lines"]]
        self.assertIn(5, cited)
        self.assertIn("The text is empty.", self.error_text(empty))
        self.assertIn("timestamped log", self.error_text(notes))
        self.assertIn("binary", self.error_text(binary))

    async def test_rejects_empty_missing_and_non_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty.log").write_text("\n")
            (root / "notes.txt").write_text("This is a meeting note.\nIt has no timestamps.\n")
            (root / "binary.log").write_bytes(b"hello\0world")
            (root / "subdir").mkdir()
            with patch.dict(os.environ, {ROOT_ENV: str(root)}):
                async with Client(create_server(), raise_exceptions=True) as client:
                    empty = await client.call_tool("analyse_log", {"path": "empty.log"})
                    missing = await client.call_tool("analyse_log", {"path": "missing.log"})
                    notes = await client.call_tool("analyse_log", {"path": "notes.txt"})
                    binary = await client.call_tool("read_log_lines", {"path": "binary.log", "start_line": 1})
                    folder = await client.call_tool("read_log_lines", {"path": "subdir", "start_line": 1})
        self.assertIn("The file is empty.", self.error_text(empty))
        self.assertIn("Not a file.", self.error_text(missing))
        self.assertIn("timestamped log", self.error_text(notes))
        self.assertIn("binary", self.error_text(binary))
        self.assertIn("Not a file.", self.error_text(folder))

    async def test_line_ranges_and_the_line_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.log").write_text(secret_log())
            with patch.dict(os.environ, {ROOT_ENV: str(root)}):
                async with Client(create_server(), raise_exceptions=True) as client:
                    low = await client.call_tool(
                        "read_log_lines",
                        {"path": "app.log", "start_line": 0, "count": 1},
                    )
                    zero = await client.call_tool(
                        "read_log_lines",
                        {"path": "app.log", "start_line": 1, "count": 0},
                    )
                    past = await client.call_tool(
                        "read_log_lines",
                        {"path": "app.log", "start_line": 8, "count": 1},
                    )
                    tail = await client.call_tool(
                        "read_log_lines",
                        {"path": "app.log", "start_line": 4, "count": 30},
                    )
                    with patch("log_and_order.mcp_server.MCP_MAX_LINE_COUNT", 2):
                        capped = await client.call_tool(
                            "read_log_lines",
                            {"path": "app.log", "start_line": 1, "count": 10},
                        )
                    analysed = await client.call_tool("analyse_log", {"path": "app.log"})
        self.assertIn("start_line must be 1", self.error_text(low))
        self.assertIn("count must be 1", self.error_text(zero))
        self.assertIn("past the end", self.error_text(past))
        fetched = self.payload(tail)
        self.assertEqual(fetched["total_lines"], 7)
        self.assertEqual([line["number"] for line in fetched["lines"]], [4, 5, 6, 7])
        secret_line = next(line for line in fetched["lines"] if line["number"] == 5)
        self.assertNotIn("supersecret", secret_line["text"])
        self.assertNotIn("ghp_", secret_line["text"])
        self.assertIn("[REDACTED]", secret_line["text"])
        self.assertEqual([line["number"] for line in self.payload(capped)["lines"]], [1, 2])
        rendered = json.dumps(self.payload(analysed))
        self.assertNotIn("supersecret", rendered)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", rendered)
        self.assertIn("[REDACTED]", rendered)

    async def test_file_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.log").write_text("x" * 50)
            with (
                patch.dict(os.environ, {ROOT_ENV: str(root)}),
                patch("log_and_order.mcp_server.MCP_MAX_INPUT_BYTES", 10),
            ):
                async with Client(create_server(), raise_exceptions=True) as client:
                    too_big = await client.call_tool("analyse_log", {"path": "big.log"})
                    text = await client.call_tool("analyse_text", {"content": "y" * 20})
        self.assertIn("larger than the configured limit", self.error_text(too_big))
        self.assertIn("larger than the configured limit", self.error_text(text))

    async def test_path_traversal_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            nested = root / "nested"
            nested.mkdir(parents=True)
            secret = base / "secret.log"
            secret.write_text("OUTSIDE_SECRET\n")
            (root / "escape.log").symlink_to(secret)
            with patch.dict(os.environ, {ROOT_ENV: str(root)}):
                async with Client(create_server(), raise_exceptions=True) as client:
                    escaped = await client.call_tool(
                        "read_log_lines",
                        {"path": "escape.log", "start_line": 1},
                    )
                    climbed = await client.call_tool(
                        "analyse_log",
                        {"path": "nested/../../secret.log"},
                    )
                    absolute = await client.call_tool(
                        "read_log_lines",
                        {"path": str(secret), "start_line": 1},
                    )
        for result in (escaped, climbed, absolute):
            message = self.error_text(result)
            self.assertIn("outside the allowed root", message)
            self.assertNotIn("OUTSIDE_SECRET", message)

    async def test_stdio_session_analyses_a_jenkins_log(self) -> None:
        env = os.environ.copy()
        env[ROOT_ENV] = str(ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "log_and_order.mcp_server"],
            env=env,
            cwd=str(ROOT),
        )
        async with Client(params, raise_exceptions=True) as client:
            self.assertEqual(client.server_info.name, "log-and-order")
            first = await client.call_tool("analyse_log", {"path": JENKINS, "use_llm": False})
            second = await client.call_tool(
                "read_log_lines",
                {"path": JENKINS, "start_line": 27, "count": 1},
            )
        report = self.payload(first)
        self.assertEqual(report["jenkins"]["primary_failure"]["source_file"], "src/server.ts")
        self.assertEqual(report["analysis"]["reason"], "disabled")
        fetched = self.payload(second)
        self.assertEqual(fetched["lines"][0]["number"], 27)
        self.assertIn("TS2322", fetched["lines"][0]["text"])

    async def test_jenkins_shell_log_does_not_return_secrets(self) -> None:
        with patch.dict(os.environ, {ROOT_ENV: str(ROOT)}):
            async with Client(create_server(), raise_exceptions=True) as client:
                result = await client.call_tool("analyse_log", {"path": SHELL, "use_llm": False})
                read = await client.call_tool(
                    "read_log_lines",
                    {"path": SHELL, "start_line": 9, "count": 3},
                )
        rendered = json.dumps(self.payload(result)) + json.dumps(self.payload(read))
        self.assertNotIn("supersecretvalue", rendered)
        self.assertNotIn("supersecrettoken", rendered)
        self.assertIn("[REDACTED]", rendered)


class McpProcessTest(unittest.TestCase):
    def test_import_writes_nothing_to_stdout(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", "import log_and_order.mcp_server"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_main_rejects_a_missing_root(self) -> None:
        missing = Path(tempfile.gettempdir()) / "log-and-order-missing-root"
        self.assertFalse(missing.exists())
        stderr = io.StringIO()
        with patch.dict(os.environ, {ROOT_ENV: str(missing)}), redirect_stderr(stderr):
            status = main()
        self.assertEqual(status, 1)
        self.assertIn("not a readable directory", stderr.getvalue())

    def test_main_rejects_a_file_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-directory"
            path.write_text("x")
            stderr = io.StringIO()
            with patch.dict(os.environ, {ROOT_ENV: str(path)}), redirect_stderr(stderr):
                status = main()
        self.assertEqual(status, 1)
        self.assertIn("not a readable directory", stderr.getvalue())

    def test_unset_root_is_the_working_directory(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=False), redirect_stderr(stderr):
            os.environ.pop(ROOT_ENV, None)
            root = configured_root(announce=True)
        self.assertEqual(root, Path.cwd().resolve())
        self.assertIn("unset", stderr.getvalue())

    def test_main_reports_a_missing_sdk(self) -> None:
        stderr = io.StringIO()
        with patch("log_and_order.mcp_server.MCPServer", None), redirect_stderr(stderr):
            status = main()
        self.assertEqual(status, 1)
        self.assertIn("mcp extra", stderr.getvalue())
