"""Model client construction. No server is contacted."""

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from log_and_order.llm.client import build_llm
from log_and_order.llm.summarization import interpret_events
from log_and_order.logs.extraction import extract_events
from log_and_order.logs.selection import select_primary


def _primary():
    text = "\n".join(
        [
            "2026-10-15T00:00:00Z INFO app: one",
            "2026-10-15T00:00:01Z INFO app: two",
            "2026-10-15T00:00:02Z INFO app: three",
            "2026-10-15T00:00:03Z INFO app: four",
            "2026-10-15T00:00:04Z ERROR app: java.lang.IllegalStateException: boom",
            "\tat com.example.App.handle(App.java:10)",
        ]
    )
    events = extract_events(text)
    assert events is not None
    primary = select_primary(events)
    assert primary is not None
    return primary


class BuildLlmTest(unittest.TestCase):
    def test_ollama_keeps_its_options(self) -> None:
        llm = build_llm(
            reasoning=False,
            num_predict=1024,
            model="local-model",
            json_mode=True,
            base_url="http://127.0.0.1:11434",
            timeout=12,
            backend="ollama",
        )
        self.assertIsInstance(llm, ChatOllama)
        self.assertEqual(llm.num_predict, 1024)
        self.assertEqual(llm.num_ctx, 8192)
        self.assertFalse(llm.reasoning)
        self.assertEqual(llm.format, "json")
        self.assertEqual(llm.base_url, "http://127.0.0.1:11434")

    def test_llama_cpp_chat_request(self) -> None:
        thinking = build_llm(
            reasoning=True,
            num_predict=8192,
            model="bonsai",
            backend="llamacpp",
            base_url="http://127.0.0.1:8080/",
        )
        self.assertIsInstance(thinking, ChatOpenAI)
        self.assertEqual(thinking.openai_api_base, "http://127.0.0.1:8080/v1")
        self.assertEqual(thinking.max_tokens, 8192)
        self.assertEqual(thinking.max_retries, 0)
        self.assertFalse(thinking.use_responses_api)
        self.assertEqual(thinking.openai_api_key.get_secret_value(), "not-needed")
        thinking_body = thinking._get_request_payload([HumanMessage(content="hi")])
        self.assertEqual(thinking_body["max_completion_tokens"], 8192)
        self.assertEqual(thinking_body["extra_body"]["max_tokens"], 8192)
        self.assertEqual(thinking_body["extra_body"]["reasoning_format"], "auto")
        self.assertNotIn("chat_template_kwargs", thinking_body["extra_body"])
        self.assertNotIn("reasoning_effort", thinking_body)
        self.assertNotIn("response_format", thinking_body)

        structured = build_llm(
            reasoning=False,
            num_predict=1024,
            json_mode=True,
            model="bonsai",
            backend="llamacpp",
            api_key="upstream-key",
        )
        self.assertEqual(structured.openai_api_base, "http://127.0.0.1:8080/v1")
        self.assertEqual(structured.openai_api_key.get_secret_value(), "upstream-key")
        body = structured._get_request_payload([HumanMessage(content="hi")])
        self.assertEqual(body["extra_body"]["max_tokens"], 1024)
        self.assertEqual(body["extra_body"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("reasoning_format", body["extra_body"])
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_completion_tokens"], 1024)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_llm(backend="mlx")


class NarrativeBudgetTest(unittest.TestCase):
    def test_llama_cpp_uses_the_larger_budgets(self) -> None:
        event = _primary()
        with (
            patch("log_and_order.llm.summarization.collect_summary", side_effect=[None, "Main failure\nBoom."]),
            patch("log_and_order.llm.summarization.build_llm", return_value=RunnableLambda(lambda _payload: "")) as built,
            redirect_stderr(io.StringIO()),
        ):
            answer = interpret_events(Path("app.log"), event, backend="llamacpp", api_key="k")
        self.assertEqual(answer, "Main failure\nBoom.")
        budgets = [call.kwargs["num_predict"] for call in built.call_args_list]
        self.assertEqual(budgets, [8192, 16384])
        self.assertTrue(all(call.kwargs["reasoning"] for call in built.call_args_list))
        self.assertTrue(all(call.kwargs["backend"] == "llamacpp" for call in built.call_args_list))

    def test_ollama_keeps_the_smaller_budgets(self) -> None:
        event = _primary()
        with (
            patch("log_and_order.llm.summarization.collect_summary", side_effect=[None, "Main failure\nBoom."]),
            patch("log_and_order.llm.summarization.build_llm", return_value=RunnableLambda(lambda _payload: "")) as built,
            redirect_stderr(io.StringIO()),
        ):
            interpret_events(Path("app.log"), event)
        budgets = [call.kwargs["num_predict"] for call in built.call_args_list]
        self.assertEqual(budgets, [2048, 4096])
        self.assertEqual(built.call_args_list[0].kwargs["backend"], "ollama")
