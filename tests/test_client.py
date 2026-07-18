import json

import pytest

from claude_batch import client, config
from claude_batch.client import LimitReached, call_claude, looks_like_limit, run_with_retries


# --- looks_like_limit ---------------------------------------------------------
def test_looks_like_limit_matches_keywords():
    assert looks_like_limit("Rate limit exceeded")
    assert looks_like_limit("HTTP 429 Too Many Requests")
    assert looks_like_limit("usage limit reached for this window")


def test_looks_like_limit_ignores_ordinary_errors():
    assert not looks_like_limit("command not found")
    assert not looks_like_limit("json parse failure")


# --- call_claude (subprocess faked) -------------------------------------------
class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.returncode = returncode
        self.pid = 4242
        self.input = None

    def communicate(self, input=None, timeout=None):
        self.input = input
        return self.stdout_text, self.stderr_text


def _fake_popen(monkeypatch, proc):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: proc)


def test_call_claude_success_and_stdin_prompt(monkeypatch):
    proc = FakeProc(stdout=json.dumps({"result": "hello", "total_cost_usd": 0.01}))
    _fake_popen(monkeypatch, proc)
    text, cost, usage = call_claude("the prompt", None, "haiku", 5)
    assert (text, cost) == ("hello", 0.01)
    assert usage["input_tokens"] == 0  # no usage block in the payload -> zeros
    # The prompt must travel over stdin, never argv (ps exposure / ARG_MAX).
    assert proc.input == "the prompt"


def test_call_claude_parses_usage(monkeypatch):
    payload = {
        "result": "hi",
        "total_cost_usd": 0.0,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
            "server_tool_use": {"web_search_requests": 0},  # nested extras ignored
        },
    }
    _fake_popen(monkeypatch, FakeProc(stdout=json.dumps(payload)))
    _, _, usage = call_claude("p", None, "haiku", 5)
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 40,
    }


def test_call_claude_append_system_prompt_flag(monkeypatch):
    # Passed through as --append-system-prompt when set; absent otherwise.
    captured = {}

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc(stdout=json.dumps({"result": "ok", "total_cost_usd": 0.0}))

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    call_claude("p", None, "haiku", 5, append_system_prompt="ADDENDUM")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--append-system-prompt") + 1] == "ADDENDUM"
    call_claude("p", None, "haiku", 5)
    assert "--append-system-prompt" not in captured["cmd"]


def test_call_claude_is_error_limit_classified(monkeypatch):
    payload = {"is_error": True, "result": "", "subtype": "usage limit reached"}
    _fake_popen(monkeypatch, FakeProc(stdout=json.dumps(payload)))
    with pytest.raises(RuntimeError, match="^limit:"):
        call_claude("p", None, "haiku", 5)


def test_call_claude_empty_result_is_error(monkeypatch):
    _fake_popen(monkeypatch, FakeProc(stdout=json.dumps({"result": ""})))
    with pytest.raises(RuntimeError, match="^error:"):
        call_claude("p", None, "haiku", 5)


def test_call_claude_non_json_output_is_error(monkeypatch):
    _fake_popen(monkeypatch, FakeProc(stdout="node exploded", returncode=1))
    with pytest.raises(RuntimeError, match="^error:"):
        call_claude("p", None, "haiku", 5)


# --- run_with_retries -----------------------------------------------------------
def test_run_with_retries_stop_on_limit_raises(monkeypatch):
    def fake_call(*a, **k):
        raise RuntimeError("limit: usage limit reached")

    monkeypatch.setattr(client, "call_claude", fake_call)
    with pytest.raises(LimitReached):
        run_with_retries("p", None, "haiku", 1, stop_on_limit=True)


def test_run_with_retries_without_stop_on_limit_backs_off(monkeypatch):
    # Without the flag, a limit is retried (not raised as LimitReached): succeed on
    # the 2nd attempt so the test stays fast and asserts the backoff path is taken.
    calls = []

    def fake_call(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("limit: usage limit reached")
        return "ok", 0.0, {}

    monkeypatch.setattr(client, "call_claude", fake_call)
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)
    assert run_with_retries("p", None, "haiku", 1) == ("ok", 0.0, {})
    assert len(calls) == 2


def test_run_with_retries_general_error_retried_then_succeeds(monkeypatch):
    calls = []

    def fake_call(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("error: transient")
        return "ok", 0.0, {}

    sleeps = []
    monkeypatch.setattr(client, "call_claude", fake_call)
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    assert run_with_retries("p", None, "haiku", 1) == ("ok", 0.0, {})
    # Exponential backoff: base, then double.
    assert sleeps == [config.GENERAL_SLEEP_BASE_S, config.GENERAL_SLEEP_BASE_S * 2]


def test_run_with_retries_general_errors_exhaust(monkeypatch):
    def fake_call(*a, **k):
        raise RuntimeError("error: persistent")

    monkeypatch.setattr(client, "call_claude", fake_call)
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "MAX_GENERAL_RETRIES", 2)
    with pytest.raises(RuntimeError, match="persistent"):
        run_with_retries("p", None, "haiku", 1)


def test_run_with_retries_limit_errors_exhaust(monkeypatch):
    def fake_call(*a, **k):
        raise RuntimeError("limit: still limited")

    monkeypatch.setattr(client, "call_claude", fake_call)
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "MAX_LIMIT_RETRIES", 2)
    with pytest.raises(RuntimeError, match="still limited"):
        run_with_retries("p", None, "haiku", 1)
