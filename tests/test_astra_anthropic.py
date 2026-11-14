#!/usr/bin/env python3
"""Claude analyst backend tests. Offline: the real `anthropic` SDK talks to a mocked HTTP
transport, so request shape and response parsing are checked without network or API key."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import astra_anthropic as AN  # noqa: E402

try:
    import anthropic
    try:
        import httpx2 as httpx            # newer SDKs ship their own HTTP stack
    except ImportError:
        import httpx
except ImportError:                       # pragma: no cover - pip install -r requirements.txt
    anthropic = None

try:
    M = AN.monitor_module(ROOT / "src")   # the bot's main module, whatever its file name
except Exception:                          # pragma: no cover - module-only checkout
    M = None

SCHEMA = {"type": "object", "properties": {"bias": {"type": "string", "enum": ["BULLISH", "BEARISH"]},
                                           "level": {"type": ["number", "null"]}},
          "required": ["bias", "level"], "additionalProperties": False}
GOOD = {"bias": "BULLISH", "level": 4012.5}


def message(content, stop="end_turn", usage=None):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": usage or {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800}}


def text(obj):
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj)}


THINK = {"type": "thinking", "thinking": "4H structure is bullish...", "signature": "sig"}


class FakeAPI:
    """Real SDK client over a mocked transport. `replies` are (status, json) in order."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        transport = httpx.MockTransport(self.handle)
        self.client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                                          http_client=httpx.Client(transport=transport))

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        status, body = self.replies.pop(0)
        return httpx.Response(status, json=body)


def bad_request(msg):
    return 400, {"type": "error", "error": {"type": "invalid_request_error", "message": msg}}


@unittest.skipIf(anthropic is None, "anthropic SDK not installed")
class CallTests(unittest.TestCase):
    def call(self, api, **kw):
        args = dict(effort="medium", max_tokens=9000, structured=True)
        args.update(kw)
        return AN.call_json(api.client, "claude-opus-5-5", SCHEMA, "You are Astra.", '{"now": 1}', **args)

    def test_request_shape(self):
        api = FakeAPI((200, message([THINK, text(GOOD)])))
        self.assertEqual(self.call(api, effort="high"), GOOD)
        body = api.requests[0]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], 9000)
        self.assertEqual(body["output_config"], {"effort": "high",
                                                 "format": {"type": "json_schema", "schema": SCHEMA}})
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["system"][0]["text"], "You are Astra.")
        self.assertEqual(body["messages"], [{"role": "user", "content": '{"now": 1}'}])
        self.assertNotIn("thinking", body)          # Opus 5.5: adaptive thinking, steered by effort
        self.assertNotIn("temperature", body)

    def test_workspace_header_when_set(self):
        captured = {}
        real = anthropic.Anthropic

        def fake(**kw):
            captured.clear()
            captured.update(kw)
            return real(**dict({"auth_token": "sk-ant-test"}, **kw))
        with patch.object(anthropic, "Anthropic", side_effect=fake):
            with patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": " wrkspc_123 ",
                                         "ANTHROPIC_API_KEY": ' "sk-ant-usr-abc" '}):
                AN.make_client({"ai_deadline_seconds": 480})
            self.assertEqual(captured["default_headers"], {"anthropic-workspace-id": "wrkspc_123"})
            self.assertEqual(captured["auth_token"], "sk-ant-usr-abc")       # Bearer, quotes/spaces removed
            self.assertNotIn("api_key", captured)
            with patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": ""}):
                AN.make_client({})
            self.assertIsNone(captured["default_headers"])

    def test_bearer_header_sent(self):
        api = FakeAPI((200, message([text(GOOD)])))
        seen = []
        transport = httpx.MockTransport(lambda r: (seen.append(dict(r.headers)), api.handle(r))[1])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-usr-xyz", "ANTHROPIC_WORKSPACE_ID": "wrkspc_9"}):
            real = anthropic.Anthropic
            with patch.object(anthropic, "Anthropic",
                              side_effect=lambda **kw: real(http_client=httpx.Client(transport=transport), **kw)):
                client = AN.make_client({})
            AN.call_json(client, "claude-opus-5-5", SCHEMA, "s", "x")
        self.assertEqual(seen[0]["authorization"], "Bearer sk-ant-usr-xyz")
        self.assertNotIn("x-api-key", seen[0])
        self.assertEqual(seen[0]["anthropic-workspace-id"], "wrkspc_9")

    def test_usage_recorded(self):
        api = FakeAPI((200, message([text(GOOD)])))
        self.call(api)
        self.assertEqual(AN.LAST_USAGE["input_tokens"], 1200)
        self.assertEqual(AN.LAST_USAGE["total_tokens"], 2300)

    def test_refusal_and_truncation_fail_closed(self):
        for stop, word in (("refusal", "declined"), ("max_tokens", "cut off")):
            api = FakeAPI((200, message([text('{"bias": "BULL')], stop=stop)))
            with self.assertRaisesRegex(AN.AnalystError, word):
                self.call(api)

    def test_schema_violation_rejected(self):
        api = FakeAPI((200, message([text({"bias": "SIDEWAYS", "level": 1})])))
        with self.assertRaises(Exception) as cm:
            self.call(api)
        self.assertEqual(type(cm.exception).__name__, "ValidationError")

    def test_empty_answer_rejected(self):
        api = FakeAPI((200, message([THINK])))
        with self.assertRaisesRegex(AN.AnalystError, "Empty"):
            self.call(api)

    def test_schema_refused_by_api_retries_in_prompt_mode(self):
        api = FakeAPI(bad_request("output_config.format: Schema is too complex for compilation"),
                      (200, message([text("Here it is:\n```json\n" + json.dumps(GOOD) + "\n```")])))
        self.assertEqual(self.call(api), GOOD)
        self.assertEqual(len(api.requests), 2)
        retry = api.requests[1]
        self.assertNotIn("format", retry["output_config"])
        self.assertEqual(retry["output_config"]["effort"], "medium")
        self.assertIn("OUTPUT FORMAT", retry["system"][0]["text"])
        self.assertIn('"enum":["BULLISH","BEARISH"]', retry["system"][0]["text"])

    def test_other_bad_request_not_retried(self):
        api = FakeAPI(bad_request("max_tokens: must be at most 64000"))
        with self.assertRaises(anthropic.BadRequestError):
            self.call(api)
        self.assertEqual(len(api.requests), 1)

    def test_prompt_mode_configured(self):
        api = FakeAPI((200, message([text(json.dumps(GOOD))])))
        self.assertEqual(self.call(api, structured=False), GOOD)
        self.assertNotIn("format", api.requests[0]["output_config"])

    def test_invalid_effort(self):
        with self.assertRaises(ValueError):
            AN.call_json(None, "m", SCHEMA, "s", "x", effort="adaptive")

    def test_image_lesson_blocks(self):
        api = FakeAPI((200, message([THINK, text(GOOD)])))
        parts = [{"type": "input_text", "text": "Lesson 001"},
                 {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}]
        result, tokens = AN.lesson_ask(api.client, {"model": "claude-opus-5-5", "provider": "anthropic"},
                                       SCHEMA, "teach", parts, "medium", 4000)
        self.assertEqual((result, tokens), (GOOD, 2300))
        body = api.requests[0]
        self.assertEqual(body["max_tokens"], 12000)
        self.assertEqual(body["messages"][0]["content"], [
            {"type": "text", "text": "Lesson 001"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}])


class HelperTests(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(AN.extract_json(json.dumps(GOOD)), GOOD)
        self.assertEqual(AN.extract_json("```json\n" + json.dumps(GOOD) + "\n```"), GOOD)
        self.assertEqual(AN.extract_json("Sure: " + json.dumps(GOOD) + " done"), GOOD)
        for bad in ("", "no json", "{broken"):
            with self.assertRaises(AN.AnalystError):
                AN.extract_json(bad)

    def test_config_defaults_and_validation(self):
        with patch.dict(os.environ, {"GOLD_ANALYST": ""}):
            c = AN.validate_config({"ai_deadline_seconds": 480})
        self.assertEqual((c["analyst_provider"], c["anthropic_model"]), ("openai", "claude-opus-5-5"))
        for bad in ({"analyst_provider": "gemini"}, {"anthropic_map_max_tokens": 500},
                    {"anthropic_review_max_tokens": True}, {"anthropic_structured_output": "yes"},
                    {"anthropic_model": ""}):
            with self.assertRaises(ValueError):
                AN.validate_config(dict(bad))

    def test_switch_precedence_and_aliases(self):
        with patch.dict(os.environ, {"GOLD_ANALYST": ""}):
            self.assertEqual(AN.validate_config({"analyst_provider": "Claude"})["analyst_provider"], "anthropic")
            self.assertEqual(AN.validate_config({"analyst_provider": "astra"})["analyst_provider"], "openai")
        with patch.dict(os.environ, {"GOLD_ANALYST": "claude"}):
            self.assertEqual(AN.validate_config({"analyst_provider": "openai"})["analyst_provider"], "anthropic")
            self.assertEqual(AN.label(AN.validate_config({})), "Claude")
        with patch.dict(os.environ, {"GOLD_ANALYST": " Astra "}):
            self.assertEqual(AN.validate_config({"analyst_provider": "anthropic"})["analyst_provider"], "openai")
        with patch.dict(os.environ, {"GOLD_ANALYST": "cluade"}):
            with self.assertRaisesRegex(ValueError, "GOLD_ANALYST"):
                AN.validate_config({})

    def test_key_name_follows_switch(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, "c.json")
            cfg.write_text(json.dumps({"analyst_provider": "openai"}), encoding="utf-8")
            with patch.dict(os.environ, {"GOLD_ANALYST": "claude"}):
                self.assertEqual(AN.analyst_key_name(str(cfg)), "ANTHROPIC_API_KEY")
                self.assertEqual(AN.analyst_key_name(str(cfg), "openai"), "OPENAI_API_KEY")   # --analyst wins

    def test_required_keys_and_modules(self):
        claude_news_off = {"analyst_provider": "anthropic", "providers": {"news": "none"}}
        claude_news_on = {"analyst_provider": "anthropic", "providers": {"news": "openai_web"}}
        openai_only = {"analyst_provider": "openai", "providers": {"news": "openai_web"}}
        self.assertEqual(AN.required_keys(claude_news_off), ["ANTHROPIC_API_KEY"])
        self.assertEqual(AN.required_keys(claude_news_on), ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
        self.assertEqual(AN.required_keys(openai_only), ["OPENAI_API_KEY"])
        self.assertEqual(AN.required_modules(claude_news_off), {"anthropic": "anthropic"})
        self.assertEqual(AN.required_modules(openai_only), {"openai": "openai"})

    def test_analyst_key_name(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GOLD_ANALYST": ""}):
            cfg = Path(d, "c.json")
            cfg.write_text(json.dumps({"analyst_provider": "anthropic"}), encoding="utf-8")
            self.assertEqual(AN.analyst_key_name(str(cfg)), "ANTHROPIC_API_KEY")
            self.assertEqual(AN.analyst_key_name(str(cfg), "openai"), "OPENAI_API_KEY")
            self.assertEqual(AN.analyst_key_name(None), "OPENAI_API_KEY")
            cfg.write_text("{not json", encoding="utf-8")
            self.assertEqual(AN.analyst_key_name(str(cfg)), "OPENAI_API_KEY")

    def test_load_analyst(self):
        with tempfile.TemporaryDirectory() as d:
            c = AN.validate_config({"analyst_provider": "anthropic"})
            self.assertEqual(AN.load_analyst(c, d)["instructions"], "")       # missing file: contract only
            Path(d, "config").mkdir()
            Path(d, "config", "astra_instructions.md").write_text("ROLE: gold analyst\n", encoding="utf-8")
            saved = AN.load_analyst(c, d)
            self.assertEqual((saved["instructions"], saved["provider"], saved["structured"]),
                             ("ROLE: gold analyst", "anthropic", True))
            with patch.dict(os.environ, {"GOLD_CLAUDE_MODEL": "claude-sonnet-5-5"}):
                self.assertEqual(AN.load_analyst(c, d)["model"], "claude-sonnet-5-5")

    def test_schema_checker(self):
        self.assertEqual(AN.schema_issues(SCHEMA), [])
        bad = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0},
                                                "a": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
               "required": ["n", "a"], "additionalProperties": True}
        issues = " ".join(AN.schema_issues(bad))
        for word in ("minimum", "maxItems", "additionalProperties"):
            self.assertIn(word, issues)


class Pipe:
    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, item):
        self.sent.append(item)

    def close(self):
        self.closed = True


class NoNews:
    def check(self, payload, client=None):
        return {"risk": "UNKNOWN", "summary": "none", "facts": [], "opinions": [], "checked_at": "t"}


@unittest.skipIf(anthropic is None, "anthropic SDK not installed")
class WorkerTests(unittest.TestCase):
    def fake_monitor(self, root):
        return types.SimpleNamespace(
            ROOT=Path(root), MAP_SCHEMA=SCHEMA, REVIEW_SCHEMA=SCHEMA, LAST_USAGE=None,
            astra_instructions=lambda saved, kind: saved["instructions"] + "|" + kind,
            build_provider=lambda kind, c: NoNews(), iso=lambda d: d.isoformat(),
            utcnow=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))

    def config(self, **kw):
        c = AN.validate_config({"analyst_provider": "anthropic", "ai_deadline_seconds": 480,
                                "astra_reasoning": "medium", "providers": {"news": "none"}})
        c.update(kw)
        return c

    def run_worker(self, kind, api, c=None):
        pipe = Pipe()
        with tempfile.TemporaryDirectory() as d, patch.object(AN, "make_client", return_value=api.client):
            fm = self.fake_monitor(d)
            AN.ai_worker(fm, kind, {"now": "t", "language": "English"}, c or self.config(), None, pipe)
        return pipe, fm

    def test_map(self):
        api = FakeAPI((200, message([THINK, text(GOOD)])))
        pipe, fm = self.run_worker("map", api)
        self.assertTrue(pipe.closed)
        kind, value = pipe.sent[-1]
        self.assertEqual(kind, "result")
        self.assertEqual((value["technical"], value["news"]), (GOOD, None))
        self.assertEqual(value["saved"]["provider"], "anthropic")
        self.assertEqual(api.requests[0]["max_tokens"], 32000)
        self.assertEqual(api.requests[0]["system"][0]["text"], "|map")
        self.assertEqual(fm.LAST_USAGE["output_tokens"], 300)

    def test_review_runs_news(self):
        api = FakeAPI((200, message([text(GOOD)])))
        pipe, _ = self.run_worker("review", api)
        kind, value = pipe.sent[-1]
        self.assertEqual(kind, "result")
        self.assertEqual(value["news"]["risk"], "UNKNOWN")
        self.assertEqual(api.requests[0]["max_tokens"], 8000)

    def test_failure_reports_type_and_reason(self):
        """6.1.4: the class alone ("BadRequestError" x43 on 14 Oct) was useless; the reason travels too."""
        api = FakeAPI((200, message([text('{"bias": "BULL')], stop="max_tokens")))
        pipe, _ = self.run_worker("map", api)
        kind, value = pipe.sent[-1]
        self.assertEqual(kind, "error")
        self.assertTrue(value.startswith("AnalystError: Answer cut off at max_tokens=32000"), value)
        self.assertTrue(pipe.closed)


@unittest.skipIf(M is None, "bot monitor module not importable")
class MonitorIntegrationTests(unittest.TestCase):
    """The Claude switch as wired into the bot's main module."""

    def test_real_schemas_are_structured_output_compatible(self):
        for name in ("MAP_SCHEMA", "REVIEW_SCHEMA", "NEWS_SCHEMA"):
            self.assertEqual(AN.schema_issues(getattr(M, name)), [], name)
        import learn_lessons as LL
        for name in ("BLIND_SCHEMA", "COMPARE_SCHEMA", "CONCEPT_BLIND_SCHEMA", "CONCEPT_COMPARE_SCHEMA",
                     "PLAYBOOK_SCHEMA"):
            self.assertEqual(AN.schema_issues(getattr(LL, name)), [], name)

    def test_config_switch(self):
        with patch.dict(os.environ, {"GOLD_ANALYST": ""}):
            self._config_switch()

    def _config_switch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"analyst_provider": "anthropic", "astra_reasoning": "high"}, f)
        self.addCleanup(lambda: Path(f.name).unlink())
        c = M.load_config(f.name)
        self.assertEqual((c["analyst_provider"], c["anthropic_model"]), ("anthropic", "claude-opus-5-5"))
        self.assertEqual(M.load_config(None)["analyst_provider"], "openai")      # default unchanged
        Path(f.name).write_text(json.dumps({"analyst_provider": "gemini"}))
        with self.assertRaises(ValueError):
            M.load_config(f.name)
        with patch.dict(os.environ, {"GOLD_ANALYST": "claude"}):
            self.assertEqual(M.load_config(None)["analyst_provider"], "anthropic")

    def test_worker_dispatch(self):
        c = M.load_config(None)
        with patch.object(AN, "ai_worker") as claude:
            c["analyst_provider"] = "anthropic"
            M.ai_worker("map", {}, c, None, Pipe())
            self.assertTrue(claude.called)
            self.assertIs(claude.call_args[0][0], M.sys_module())

    def test_secret_redaction(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret-123"}):
            self.assertNotIn("sk-ant-secret-123", M.safe_error("bad key sk-ant-secret-123"))


class MonitorFinderTests(unittest.TestCase):
    def test_finds_renamed_module(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "helper.py").write_text("X = 1\n", encoding="utf-8")
            Path(d, "gold_bot_x9.py").write_text(AN.MONITOR_SIGNATURE + "\n    pass\nNAME = 'renamed'\n",
                                                 encoding="utf-8")
            try:
                self.assertEqual(AN.monitor_module(d).NAME, "renamed")
            finally:
                sys.modules.pop("gold_bot_x9", None)
                sys.path.remove(str(Path(d)))
            Path(d, "gold_bot_x9.py").unlink()
            with self.assertRaises(ImportError):
                AN.monitor_module(d)


if __name__ == "__main__":
    unittest.main()
