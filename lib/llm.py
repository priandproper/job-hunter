"""Phase 15 — a thin, robust provider abstraction over the Claude CLI.

Every LLM call in this project should go through here so the invocation is uniformly
guarded: a timeout, exit-status capture, envelope parsing, JSON extraction, and
PII-safe errors (the prompt — which can contain résumé content — is NEVER echoed in an
error message or log). This is also the seam for swapping the provider later.

OPERATIONAL FRAGILITY (documented, not a legal claim): the default provider drives the
logged-in `claude` CLI headlessly. That depends on an interactive login/session staying
valid and is subject to being logged out or rate-limited; it is not a stable programmatic
interface. Callers MUST handle LLMError by degrading (see coach_rank's deterministic
fallback) so the app stays useful when Claude is unavailable. To move to a hosted API
later, add a provider that implements run_json() with the same contract and select it
here — nothing else needs to change.
"""

import json
import re
import shutil
import subprocess

DEFAULT_CMD = "claude"


class LLMError(Exception):
    """Any failure invoking the model or getting usable JSON back."""


class LLMUnavailable(LLMError):
    """The model provider isn't available at all (CLI missing / not logged in)."""


def available(cmd=DEFAULT_CMD) -> bool:
    return shutil.which(cmd) is not None


def _extract_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise LLMError("model returned no JSON object")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"model JSON did not parse: {e}")


def run_json(prompt: str, *, model: str = "opus", timeout: int = 600,
             cmd: str = DEFAULT_CMD) -> dict:
    """Run the model on `prompt` and return the parsed JSON object.

    Raises LLMUnavailable if the CLI is missing, or LLMError on timeout, non-zero exit,
    or unparseable output. Error messages never include the prompt (PII-safe)."""
    if not available(cmd):
        raise LLMUnavailable(f"{cmd} CLI not found on PATH (logged out or not installed)")
    try:
        proc = subprocess.run(
            [cmd, "-p", "--model", model, "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LLMError(f"{cmd} timed out after {timeout}s")
    except OSError as e:
        raise LLMError(f"{cmd} could not be launched: {e}")
    if proc.returncode != 0:
        # stderr only (bounded); the prompt is never surfaced.
        err = (proc.stderr or "").strip().splitlines()
        raise LLMError(f"{cmd} exited {proc.returncode}: " + (err[-1] if err else "no stderr"))
    out = (proc.stdout or "").strip()
    if not out:
        raise LLMError(f"{cmd} returned an empty response (likely declined or logged out)")
    # The CLI wraps the result in a JSON envelope {..., "result": "<text>"}.
    try:
        env = json.loads(out)
        text = env.get("result", out) if isinstance(env, dict) else out
    except json.JSONDecodeError:
        text = out
    return _extract_json(text)
