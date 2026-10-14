"""The reasoning layer: headless Claude via `claude -p`.

* Auth: CLAUDE_KEY (a Claude Code token, not an Anthropic API key) is passed to the CLI
  as CLAUDE_CODE_OAUTH_TOKEN.
* Cost control: a content-addressed disk cache (same prompt -> no new call), a per-run
  call budget, and a hard timeout. Every failure returns None so callers fall back to rules.
* Privacy: optional redaction of emails / phone numbers before text leaves the machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")


def redact(text: str) -> str:
    return PHONE_RE.sub("[PHONE]", EMAIL_RE.sub("[EMAIL]", text))


def claude_available() -> bool:
    """True when `claude` is installed and has credentials (CLAUDE_KEY), or LEXFLOW_LLM=claude
    says the CLI is already logged in some other way."""
    return shutil.which("claude") is not None and bool(
        os.getenv("CLAUDE_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("LEXFLOW_LLM") == "claude")


def extract_json(text: str | None):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = m.group(1) if m else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    seconds: float = 0.0
    budget_exhausted: int = 0


@dataclass
class ClaudeCLI:
    model: str | None = None
    timeout: float = 120.0
    cache_dir: Path | None = None
    max_calls: int = 500
    redact_pii: bool = False
    stats: LLMStats = field(default_factory=LLMStats)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _cache_path(self, prompt: str) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(f"{self.model}|{prompt}".encode()).hexdigest()[:32]
        return self.cache_dir / f"{key}.json"

    def complete(self, prompt: str) -> str | None:
        if self.redact_pii:
            prompt = redact(prompt)
        cp = self._cache_path(prompt)
        if cp and cp.exists():
            with self._lock:
                self.stats.cache_hits += 1
            return json.loads(cp.read_text())["text"]
        with self._lock:
            if self.stats.calls >= self.max_calls:
                self.stats.budget_exhausted += 1
                return None
            self.stats.calls += 1
        text = self._run(prompt)
        if text is None:
            with self._lock:
                self.stats.failures += 1
            return None
        if cp:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps({"text": text}))
        return text

    def complete_json(self, prompt: str) -> dict | None:
        data = extract_json(self.complete(prompt))
        return data if isinstance(data, dict) else None

    def _run(self, prompt: str) -> str | None:
        if shutil.which("claude") is None:
            return None
        env = dict(os.environ)
        if env.get("CLAUDE_KEY") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = env["CLAUDE_KEY"]
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        if self.model:
            cmd += ["--model", self.model]
        t = time.perf_counter()
        try:
            # Empty cwd: the CLI sees no repo files, and has no tools it could act with on our data.
            with tempfile.TemporaryDirectory() as cwd:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env, cwd=cwd)
        except (subprocess.TimeoutExpired, OSError):
            return None
        finally:
            with self._lock:
                self.stats.seconds += time.perf_counter() - t
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None
