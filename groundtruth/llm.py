"""Claude, for the two jobs that need a model: live evals and wiki compiles.

Everything else in groundtruth runs without an API key. This module is only
imported when you ask for ``./gt eval --tier live``, ``./gt ask`` with a key, or
``./gt compile --write``.

Credentials come from the Anthropic SDK's normal lookup (ANTHROPIC_API_KEY, or a
profile from ``ant auth login``). Models are configurable:

    GT_MODEL         the model your assistant runs on   (default claude-opus-5-5)
    GT_JUDGE_MODEL   grades narrative answers in evals  (default claude-opus-5-5)
    GT_EFFORT        low | medium | high | xhigh | max  (default medium)

Run evals on the same model you use in production, or you're grading a
different assistant than your users talk to.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(Exception):
    pass


def model() -> str:
    return os.environ.get("GT_MODEL") or os.environ.get("EVAL_MODEL") or DEFAULT_MODEL


def judge_model() -> str:
    return os.environ.get("GT_JUDGE_MODEL") or os.environ.get("EVAL_JUDGE_MODEL") or DEFAULT_MODEL


def effort() -> str:
    return os.environ.get("GT_EFFORT", "medium")


def available() -> bool:
    """True when the SDK is installed and some credential is configured."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return bool(os.environ.get("ANTHROPIC_PROFILE")) or os.path.isdir(
        os.path.expanduser("~/.config/anthropic"))


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("the Anthropic SDK isn't installed: run ./gt setup") from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - missing credentials surface here
        raise LLMUnavailable(
            "no Anthropic credentials. Set ANTHROPIC_API_KEY (console.anthropic.com) "
            "or run `ant auth login`"
        ) from exc


def _create(client, **kwargs):
    """messages.create with the server-side refusal fallback turned on.

    If the model declines a request, the API re-runs it on a fallback model in
    the same call instead of failing the eval.
    """
    return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)


def _text(resp) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text").strip()


def agent(question: str, system: str, tools: list[dict], dispatch: Callable[[str, dict], Any],
          *, model_name: str | None = None, max_turns: int = 10) -> dict:
    """Run one question through Claude with your tools until it stops calling them."""
    client = _client()
    model_name = model_name or model()
    messages: list[dict] = [{"role": "user", "content": question}]
    calls: list[dict] = []
    tool_defs = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                 for t in tools]
    for _ in range(max_turns):
        resp = _create(
            client, model=model_name, max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tool_defs, messages=messages, output_config={"effort": effort()},
        )
        if resp.stop_reason == "refusal":
            return {"response": "[refused]", "tool_calls": calls, "stop_reason": "refusal"}
        used = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use" or not used:
            return {"response": _text(resp), "tool_calls": calls, "stop_reason": resp.stop_reason}
        results = []
        for block in used:
            args = dict(block.input or {})
            calls.append({"name": block.name, "input": args})
            try:
                out, is_error = dispatch(block.name, args), False
            except Exception as exc:  # noqa: BLE001 - the model should see tool errors
                out, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(out, default=str), "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return {"response": "[stopped: too many tool turns]", "tool_calls": calls, "stop_reason": "max_turns"}


JUDGE_SYSTEM = """You grade one answer from a grounded assistant against a rubric.
Pass it if it answers the question, covers the rubric's substantive points (wording
may differ), and doesn't invent or contradict facts. The assistant read official
sources you can't see, so don't fail plausible operational details unless they
contradict the rubric.
Reply with only JSON: {"pass": true|false, "reason": "<one sentence>"}"""


def judge(question: str, rubric: str, answer: str, *, model_name: str | None = None) -> tuple[bool, str]:
    client = _client()
    resp = _create(
        client, model=model_name or judge_model(), max_tokens=16000,
        system=[{"type": "text", "text": JUDGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"## Question\n{question}\n\n## Rubric\n{rubric}\n\n## Answer\n{answer}"}],
        output_config={"effort": "low"},
    )
    if resp.stop_reason == "refusal":
        return False, "judge refused"
    text = _text(resp)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        verdict = json.loads(m.group(0) if m else text)
    except (ValueError, AttributeError):
        return False, f"judge returned non-JSON: {text[:160]}"
    return bool(verdict.get("pass")), str(verdict.get("reason") or "")


def complete(system: str, prompt: str, *, model_name: str | None = None) -> str:
    """One plain completion (used to compile wiki pages)."""
    client = _client()
    with client.beta.messages.stream(
        betas=[FALLBACK_BETA], fallbacks="default",
        model=model_name or model(), max_tokens=64000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": os.environ.get("GT_COMPILE_EFFORT", "high")},
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise LLMUnavailable("the model declined to compile this page")
    return _text(resp)
