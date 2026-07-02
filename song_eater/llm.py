"""Optional AI assist, via the local `claude` CLI by default.

The retag pass is fully functional without this — `judge()` returns None when
no backend is available or the call fails, and callers fall back to their
deterministic result. AI is a pure enhancement layer, never a requirement.

Backend selection (env var SONG_EATER_LLM):
    claude-cli  (default)  shell out to `claude -p`
    off                    disable AI entirely
Future: anthropic (ANTHROPIC_API_KEY), openrouter.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess


def available() -> bool:
    """True if an AI backend is configured and reachable."""
    backend = os.environ.get("SONG_EATER_LLM", "claude-cli").lower()
    if backend == "off":
        return False
    if backend == "claude-cli":
        return shutil.which("claude") is not None
    return False


def judge(prompt: str, timeout: float = 90.0) -> str | None:
    """Send a prompt to the AI backend and return its raw text reply, or None
    if AI is unavailable or the call fails. Never raises."""
    backend = os.environ.get("SONG_EATER_LLM", "claude-cli").lower()
    if backend == "off":
        return None
    if backend == "claude-cli":
        return _claude_cli(prompt, timeout)
    return None


# Replace Claude Code's agentic system prompt with a plain responder and block
# all tools — otherwise `claude -p` runs as a full coding agent (reads the cwd's
# CLAUDE.md, tries file writes, ~85s per call). Constrained, it's ~4s and clean.
_JUDGE_SYSTEM = (
    "You are a strict JSON responder for a music-tagging tool. "
    "Output only the requested compact JSON object — no prose, no code fence. "
    "You have no tools and must not attempt to use any."
)


def _claude_cli(prompt: str, timeout: float) -> str | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-p", prompt,
             "--system-prompt", _JUDGE_SYSTEM,
             "--disallowed-tools", "*"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def judge_json(prompt: str, timeout: float = 90.0) -> dict | list | None:
    """Like judge(), but parse the reply as JSON. Tolerates a stray code fence
    or surrounding prose by extracting the first {...} / [...] block."""
    raw = judge(prompt, timeout)
    if not raw:
        return None
    for candidate in (raw, _extract_json(raw)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_json(text: str) -> str | None:
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return None
    return text[start:end + 1]
