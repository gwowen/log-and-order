"""HTTP server. Ollama is mocked. The Jenkins upload still uses the real extractor."""

import io
import logging
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from log_and_order.api.app import create_app
from log_and_order.api.ollama import describe_target
from log_and_order.api.settings import ApiSettings, SettingsError, load_settings
from log_and_order.cli import main
from log_and_order.config import MODEL_NAME
from log_and_order.mcp_server import analyse_content
from tests.test_enrichment import FakeModel, PromptModel, patch_llm

logging.getLogger("log_and_order.api").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "example-logs"
NOISY = LOGS / "jenkins_noisy_build_failure.log"
SHELL = LOGS / "jenkins_shell_failure.log"
MODEL = "LiquidAI/lfm2.5-2.6b"
OLLAMA = "http://ollama.internal:11434"
LLAMA = "http://llama.internal:8080"


def api_settings(**overrides: object) -> ApiSettings:
    values: dict[str, object] = {
        "ollama_base_url": OLLAMA,
        "model": MODEL,
        "api_key": None,
        "max_upload_bytes": 32 * 1024 * 1024,
        "ollama_timeout": 30.0,
        "max_concurrent": 1,
        "host": "127.0.0.1",
        "port": 8080,
    }
    values.update(overrides)
    return ApiSettings(**values)  # type: ignore[arg-type]


def tags_factory(names: list[str] | None = None, error: Exception | None = None):
    """A client factory whose only answer is GET /api/tags."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/tags":
            return httpx.Response(404, json={"error": "not found"})
        if error is not None:
            raise error
        listed = names if names is not None else [f"{MODEL}:latest"]
        return httpx.Response(200, json={"models": [{"name": name} for name in listed]})

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


def llama_factory(
    model_id: str = "models/bonsai.gguf",
    *,
    health: int = 200,
    models_status: int = 200,
    error: Exception | None = None,
    seen: list | None = None,
):
    """A client factory for /health and /v1/models."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.url.path, request.headers.get("authorization")))
        if error is not None:
            raise error
        if request.url.path == "/health":
            if health == 503:
                return httpx.Response(503, json={"error": {"message": "Loading model"}})
            return httpx.Response(health, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                models_status,
                json={"object": "list", "data": [{"id": model_id, "object": "model"}]},
            )
        return httpx.Response(404, json={"error": "not found"})

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


def secret_log() -> bytes:
    lines = [
        "2024-01-01T00:00:01Z INFO app : started",
        "2024-01-01T00:00:02Z INFO app : ready",
        "2024-01-01T00:00:03Z INFO app : listening",
        "2024-01-01T00:00:04Z INFO app : serving",
        "2024-01-01T00:00:05Z ERROR app : password=supersecret failed",
        "java.lang.IllegalStateException: boom",
        "\tat com.example.App.handle(App.java:12)",
    ]
    return ("\n".join(lines) + "\n").encode()


class ApiSettingsTest(unittest.TestCase):
    def test_missing_url_and_model_are_reported_together(self) -> None:
        with self.assertRaises(SettingsError) as caught:
            load_settings({})
        message = str(caught.exception)
        self.assertIn("OLLAMA_BASE_URL", message)
        self.assertIn("LOG_AND_ORDER_MODEL", message)

    def test_url_must_be_an_http_origin(self) -> None:
        for url in ("ftp://ollama.internal", "http://", "file:///tmp/ollama", "http://ollama.internal/api"):
            with self.assertRaises(SettingsError):
                load_settings({"OLLAMA_BASE_URL": url, "LOG_AND_ORDER_MODEL": MODEL})

    def test_valid_settings_keep_defaults(self) -> None:
        settings = load_settings({"OLLAMA_BASE_URL": "http://192.168.1.100:11434/", "LOG_AND_ORDER_MODEL": MODEL})
        self.assertEqual(settings.ollama_base_url, "http://192.168.1.100:11434")
        self.assertEqual(settings.model, MODEL)
        self.assertIsNone(settings.api_key)
        self.assertEqual(settings.max_upload_bytes, 32 * 1024 * 1024)
        self.assertEqual(settings.ollama_timeout, 120.0)
        self.assertEqual(settings.max_concurrent, 1)
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8080)
        self.assertEqual(settings.backend, "ollama")
        self.assertEqual(settings.llama_base_url, "")
        self.assertIsNone(settings.llama_api_key)

    def test_llama_cpp_settings_require_an_origin(self) -> None:
        with self.assertRaises(SettingsError) as caught:
            load_settings({"LOG_AND_ORDER_BACKEND": "llamacpp", "LOG_AND_ORDER_MODEL": "bonsai"})
        self.assertIn("LLAMA_BASE_URL", str(caught.exception))
        self.assertNotIn("OLLAMA_BASE_URL", str(caught.exception))
        for url in ("ftp://llama.internal", "http://llama.internal/v1", "http://llama.internal/health"):
            with self.assertRaises(SettingsError):
                load_settings(
                    {
                        "LOG_AND_ORDER_BACKEND": "llamacpp",
                        "LLAMA_BASE_URL": url,
                        "LOG_AND_ORDER_MODEL": "bonsai",
                    }
                )
        settings = load_settings(
            {
                "LOG_AND_ORDER_BACKEND": "llamacpp",
                "LLAMA_BASE_URL": "http://127.0.0.1:8080/",
                "LLAMA_API_KEY": "upstream-key",
                "LOG_AND_ORDER_MODEL": "bonsai",
            }
        )
        self.assertEqual(settings.backend, "llamacpp")
        self.assertEqual(settings.llama_base_url, "http://127.0.0.1:8080")
        self.assertEqual(settings.llama_api_key, "upstream-key")
        self.assertEqual(settings.ollama_base_url, "")
        self.assertEqual(settings.model, "bonsai")

    def test_main_exits_when_configuration_is_missing(self) -> None:
        from log_and_order.api.__main__ import main as serve

        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            status = serve()
        self.assertEqual(status, 1)
        self.assertIn("OLLAMA_BASE_URL", stderr.getvalue())
        self.assertIn("LOG_AND_ORDER_MODEL", stderr.getvalue())

    def test_describe_target_drops_userinfo(self) -> None:
        self.assertEqual(
            describe_target("https://user:secret@ollama.internal:11434"),
            "https://ollama.internal:11434",
        )


class HttpApiTest(unittest.TestCase):
    def test_health_does_not_call_ollama(self) -> None:
        def factory() -> httpx.Client:
            raise AssertionError("health contacted Ollama")

        with TestClient(create_app(api_settings(), factory)) as client:
            response = client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_ready_accepts_a_tagged_model_name(self) -> None:
        with TestClient(create_app(api_settings(), tags_factory())) as client:
            response = client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready", "model": MODEL, "reason": "ok"})
        self.assertNotIn("ollama.internal", response.text)

    def test_ready_reports_an_unreachable_server(self) -> None:
        factory = tags_factory(error=httpx.ConnectError("refused"))
        with TestClient(create_app(api_settings(), factory)) as client:
            response = client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "unreachable")
        self.assertEqual(response.json()["status"], "not_ready")

    def test_ready_reports_a_missing_model(self) -> None:
        factory = tags_factory(["some-other-model:latest"])
        with TestClient(create_app(api_settings(), factory)) as client:
            response = client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "model_missing")

    def test_models_require_the_configured_key_and_list_names(self) -> None:
        settings = api_settings(api_key="example-secret")
        with TestClient(create_app(settings, tags_factory(["alpha:latest", "beta"]))) as client:
            open_health = client.get("/api/v1/health")
            missing = client.get("/api/v1/models")
            wrong = client.get("/api/v1/models", headers={"Authorization": "Bearer wrong-secret"})
            allowed = client.get("/api/v1/models", headers={"Authorization": "Bearer example-secret"})
        self.assertEqual(open_health.status_code, 200)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(missing.json()["error"], "unauthorized")
        self.assertNotIn("example-secret", missing.text)
        self.assertEqual(missing.headers["www-authenticate"], "Bearer")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["models"], ["alpha:latest", "beta"])

    def test_models_when_ollama_is_down(self) -> None:
        factory = tags_factory(error=httpx.ConnectError("refused"))
        with TestClient(create_app(api_settings(), factory)) as client:
            response = client.get("/api/v1/models")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "ollama_unavailable")

    def test_llama_ready_ignores_a_different_served_id(self) -> None:
        seen: list = []
        settings = api_settings(
            backend="llamacpp",
            ollama_base_url="",
            llama_base_url=LLAMA,
            model="bonsai",
        )
        with TestClient(create_app(settings, llama_factory(seen=seen))) as client:
            response = client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready", "model": "bonsai", "reason": "ok"})
        self.assertEqual([path for path, _auth in seen], ["/health"])

    def test_llama_models_lists_the_served_id(self) -> None:
        seen: list = []
        settings = api_settings(
            backend="llamacpp",
            ollama_base_url="",
            llama_base_url=LLAMA,
            llama_api_key="upstream-key",
            model="bonsai",
            api_key="example-secret",
        )
        with TestClient(create_app(settings, llama_factory("models/bonsai.gguf", seen=seen))) as client:
            response = client.get(
                "/api/v1/models",
                headers={"Authorization": "Bearer example-secret"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["models/bonsai.gguf"])
        self.assertIn(("/v1/models", "Bearer upstream-key"), seen)

    def test_llama_loading_and_unreachable_are_not_a_model_list(self) -> None:
        settings = api_settings(backend="llamacpp", ollama_base_url="", llama_base_url=LLAMA, model="bonsai")
        with TestClient(create_app(settings, llama_factory(health=503))) as client:
            ready = client.get("/api/v1/ready")
            models = client.get("/api/v1/models")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["reason"], "loading")
        self.assertEqual(models.status_code, 503)
        self.assertEqual(models.json()["error"], "llama_unavailable")
        self.assertIn("loading", models.json()["detail"])

        with TestClient(create_app(settings, llama_factory(error=httpx.ConnectError("refused")))) as client:
            ready = client.get("/api/v1/ready")
            models = client.get("/api/v1/models")
        self.assertEqual(ready.json()["reason"], "unreachable")
        self.assertEqual(models.status_code, 503)
        self.assertEqual(models.json()["error"], "llama_unavailable")

    def test_llama_upload_uses_the_configured_origin(self) -> None:
        settings = api_settings(
            backend="llamacpp",
            ollama_base_url="",
            llama_base_url=LLAMA,
            llama_api_key="upstream-key",
            model="bonsai",
            ollama_timeout=15,
        )
        with (
            patch_llm(return_value=FakeModel(["{}"])) as built,
            redirect_stderr(io.StringIO()),
            TestClient(create_app(settings, llama_factory())) as client,
        ):
            response = client.post(
                "/api/v1/analyze",
                files={"file": ("jenkins_shell_failure.log", SHELL.read_bytes(), "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(built.call_args.kwargs["backend"], "llamacpp")
        self.assertEqual(built.call_args.kwargs["base_url"], LLAMA)
        self.assertEqual(built.call_args.kwargs["api_key"], "upstream-key")
        self.assertEqual(built.call_args.kwargs["model"], "bonsai")
        self.assertEqual(built.call_args.kwargs["timeout"], 15)

    def test_rejects_empty_binary_encoding_and_oversize_uploads(self) -> None:
        settings = api_settings(max_upload_bytes=64)
        with TestClient(create_app(settings, tags_factory())) as client:
            missing = client.post("/api/v1/analyze", files={"note": ("a.txt", b"hello", "text/plain")})
            empty = client.post("/api/v1/analyze", files={"file": ("empty.log", b" \n", "text/plain")})
            binary = client.post("/api/v1/analyze", files={"file": ("bin.log", b"abc\0def", "text/plain")})
            encoded = client.post("/api/v1/analyze", files={"file": ("bad.log", b"\xff", "application/octet-stream")})
            huge = client.post("/api/v1/analyze", files={"file": ("big.log", b"a" * 65, "text/plain")})
            notes = client.post("/api/v1/analyze", files={"file": ("notes.txt", b"just a note\n", "text/plain")})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["detail"], "The file is empty.")
        self.assertEqual(binary.status_code, 400)
        self.assertIn("binary", binary.json()["detail"])
        self.assertEqual(encoded.status_code, 400)
        self.assertIn("UTF-8", encoded.json()["detail"])
        self.assertEqual(huge.status_code, 413)
        self.assertEqual(huge.json()["error"], "payload_too_large")
        self.assertEqual(notes.status_code, 422)
        self.assertEqual(notes.json()["error"], "unsupported_log")

    def test_jenkins_upload_uses_the_configured_ollama_and_keeps_line_numbers(self) -> None:
        payload = NOISY.read_bytes()
        with (
            patch_llm(return_value=PromptModel()) as built,
            redirect_stderr(io.StringIO()),
            TestClient(create_app(api_settings(), tags_factory())) as client,
        ):
            response = client.post(
                "/api/v1/analyze",
                files={"file": ("../../jenkins_noisy_build_failure.log", payload, "text/plain")},
                data={"ollama_url": "http://169.254.169.254:11434", "model": "other-model"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["source"], "jenkins_noisy_build_failure.log")
        primary = body["jenkins"]["primary_failure"]
        self.assertEqual(primary["source_file"], "src/services/tenantResolver.ts")
        self.assertEqual(primary["source_line"], 147)
        self.assertIn(3694, primary["log_lines"])
        failure = next(line for line in body["evidence"]["lines"] if line["role"] == "failure")
        self.assertEqual(failure["number"], 3694)
        self.assertIn("TS2345", failure["text"])
        self.assertTrue(body["analysis"]["enriched"])
        self.assertFalse(body["analysis"]["fallback"])
        self.assertEqual(body["analysis"]["failure_mechanism"], "The logged call failed with the recorded exception.")
        self.assertEqual(built.call_args.kwargs["model"], MODEL)
        self.assertEqual(built.call_args.kwargs["base_url"], OLLAMA)
        self.assertEqual(built.call_args.kwargs["timeout"], 30.0)
        self.assertNotIn("169.254.169.254", response.text)

    def test_connection_and_timeout_keep_the_factual_report(self) -> None:
        payload = SHELL.read_bytes()
        cases = (
            ConnectionError("refused"),
            httpx.TimeoutException("timed out"),
        )
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch_llm(return_value=FakeModel([failure])),
                    redirect_stderr(io.StringIO()),
                    TestClient(create_app(api_settings(), tags_factory())) as client,
                ):
                    response = client.post(
                        "/api/v1/analyze",
                        files={"file": ("jenkins_shell_failure.log", payload, "text/plain")},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["jenkins"]["failed_stage"], "Migrate")
                self.assertEqual(body["jenkins"]["failure_category"], "INFRASTRUCTURE")
                self.assertTrue(body["analysis"]["fallback"])
                self.assertFalse(body["analysis"]["enriched"])
                self.assertEqual(body["analysis"]["reason"], "unavailable")
                self.assertIsNone(body["analysis"]["failure_mechanism"])
                self.assertEqual(body["analysis"]["hypotheses"], [])

    def test_upload_is_redacted_before_the_report(self) -> None:
        with (
            patch_llm(return_value=FakeModel(["{}"])),
            redirect_stderr(io.StringIO()),
            TestClient(create_app(api_settings(), tags_factory())) as client,
        ):
            response = client.post(
                "/api/v1/analyze",
                files={"file": ("app.log", secret_log(), "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("supersecret", response.text)
        self.assertIn("[REDACTED]", response.text)

    def test_analyze_requires_a_bearer_token_when_configured(self) -> None:
        settings = api_settings(api_key="example-secret")
        with (
            patch_llm(return_value=FakeModel(["{}"])),
            redirect_stderr(io.StringIO()),
            TestClient(create_app(settings, tags_factory())) as client,
        ):
            denied = client.post(
                "/api/v1/analyze",
                files={"file": ("app.log", secret_log(), "text/plain")},
            )
            allowed = client.post(
                "/api/v1/analyze",
                headers={"Authorization": "Bearer example-secret"},
                files={"file": ("app.log", secret_log(), "text/plain")},
            )
        self.assertEqual(denied.status_code, 401)
        self.assertNotIn("example-secret", denied.text)
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_internal_failure_does_not_echo_the_upload(self) -> None:
        with (
            patch("log_and_order.api.routes.incident_payload", side_effect=RuntimeError("password=supersecret")),
            redirect_stderr(io.StringIO()),
            TestClient(create_app(api_settings(), tags_factory())) as client,
        ):
            response = client.post(
                "/api/v1/analyze",
                files={"file": ("app.log", secret_log(), "text/plain")},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "analysis_failed")
        self.assertNotIn("supersecret", response.text)


class OllamaConfigForwardingTest(unittest.TestCase):
    def test_cli_flag_wins_and_environment_is_the_fallback(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "LOG_AND_ORDER_BACKEND": "ollama",
                    "OLLAMA_BASE_URL": "http://env.example:11434",
                    "LOG_AND_ORDER_MODEL": "env-model",
                },
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = main(
                [
                    "--format",
                    "json",
                    "--model",
                    "flag-model",
                    "--ollama-url",
                    "http://flag.example:11434",
                    str(SHELL),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(patched.call_args.kwargs["model"], "flag-model")
        self.assertEqual(patched.call_args.kwargs["base_url"], "http://flag.example:11434")

        with (
            patch.dict(
                os.environ,
                {
                    "LOG_AND_ORDER_BACKEND": "ollama",
                    "OLLAMA_BASE_URL": "http://env.example:11434",
                    "LOG_AND_ORDER_MODEL": "env-model",
                },
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = main(["--format", "json", str(SHELL)])
        self.assertEqual(status, 0)
        self.assertEqual(patched.call_args.kwargs["model"], "env-model")
        self.assertEqual(patched.call_args.kwargs["base_url"], "http://env.example:11434")

    def test_cli_without_configuration_keeps_the_local_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"OLLAMA_BASE_URL": "", "LOG_AND_ORDER_MODEL": "", "LOG_AND_ORDER_BACKEND": ""},
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = main(["--format", "json", str(SHELL)])
        self.assertEqual(status, 0)
        self.assertIsNone(patched.call_args.kwargs["model"])
        self.assertIsNone(patched.call_args.kwargs["base_url"])
        self.assertIsNone(patched.call_args.kwargs["timeout"])

    def test_mcp_uses_the_environment_and_not_a_caller_supplied_url(self) -> None:
        text = SHELL.read_text()
        with (
            patch.dict(
                os.environ,
                {
                    "LOG_AND_ORDER_BACKEND": "ollama",
                    "OLLAMA_BASE_URL": "http://mcp.example:11434",
                    "LOG_AND_ORDER_MODEL": "mcp-model",
                },
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stderr(io.StringIO()),
        ):
            analyse_content(
                Path("jenkins_shell_failure.log"),
                text,
                use_llm=True,
                model="tool-model",
                detail="full",
                empty_message="The file is empty.",
            )
        self.assertEqual(patched.call_args.kwargs["model"], "tool-model")
        self.assertEqual(patched.call_args.kwargs["base_url"], "http://mcp.example:11434")
        self.assertEqual(patched.call_args.kwargs["backend"], "ollama")
        self.assertIsNone(patched.call_args.kwargs["api_key"])
        self.assertEqual(MODEL_NAME, "LiquidAI/lfm2.5-2.6b")

    def test_cli_and_mcp_forward_llama_cpp(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "LOG_AND_ORDER_BACKEND": "llamacpp",
                    "LLAMA_BASE_URL": "http://env.example:8080",
                    "LLAMA_API_KEY": "env-key",
                    "OLLAMA_BASE_URL": "http://ollama.example:11434",
                    "LOG_AND_ORDER_MODEL": "bonsai",
                },
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = main(
                [
                    "--format",
                    "json",
                    "--backend",
                    "llamacpp",
                    "--llama-url",
                    "http://flag.example:9931",
                    "--model",
                    "bonsai",
                    str(SHELL),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(patched.call_args.kwargs["backend"], "llamacpp")
        self.assertEqual(patched.call_args.kwargs["base_url"], "http://flag.example:9931")
        self.assertEqual(patched.call_args.kwargs["api_key"], "env-key")
        self.assertEqual(patched.call_args.kwargs["model"], "bonsai")

        text = SHELL.read_text()
        with (
            patch.dict(
                os.environ,
                {
                    "LOG_AND_ORDER_BACKEND": "llamacpp",
                    "LLAMA_BASE_URL": "http://mcp.example:8080",
                    "LLAMA_API_KEY": "mcp-key",
                    "OLLAMA_BASE_URL": "http://ollama.example:11434",
                },
            ),
            patch_llm(return_value=FakeModel(["{}"])) as patched,
            redirect_stderr(io.StringIO()),
        ):
            analyse_content(
                Path("jenkins_shell_failure.log"),
                text,
                use_llm=True,
                model=None,
                detail="full",
                empty_message="The file is empty.",
            )
        self.assertEqual(patched.call_args.kwargs["backend"], "llamacpp")
        self.assertEqual(patched.call_args.kwargs["base_url"], "http://mcp.example:8080")
        self.assertEqual(patched.call_args.kwargs["api_key"], "mcp-key")

    def test_no_llm_does_not_call_llama_cpp(self) -> None:
        with (
            patch.dict(os.environ, {"LOG_AND_ORDER_BACKEND": "llamacpp"}),
            patch_llm(side_effect=AssertionError("model called")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = main(["--format", "json", "--no-llm", "--backend", "llamacpp", str(SHELL)])
        self.assertEqual(status, 0)
