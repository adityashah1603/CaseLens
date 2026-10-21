"""The one model router. Every LLM / VLM call in CaseLens goes through `ModelRouter`.

Providers are swappable by config (CASELENS_LLM_PROVIDER):
* `gemini` (default): Google AI Studio free tier, key in CASELENS_GEMINI_API_KEY. Data sent is
  SYNTHETIC demo evidence only.
* `ollama`: local, free, no key (gemma3:4b). Too slow on a CPU-only laptop for a full ingest.
* `fake`: deterministic canned responses for tests.

Output is always JSON validated against a Pydantic model. Evidence text belongs in the `user`
message inside delimited blocks (see `evidence_block`); it is data, never instructions.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

Task = Literal["extract", "caption", "adjudicate", "plan", "synthesize", "judge"]
Provider = Literal["gemini", "ollama", "fake"]
T = TypeVar("T", bound=BaseModel)

# Cheap fast model for extraction / captions / adjudication; stronger one for plan + synthesis.
DEFAULT_MODELS: dict[Provider, dict[Task, str]] = {
    "gemini": {
        "extract": "gemini-3.5-flash-lite",
        "caption": "gemini-3.5-flash-lite",
        "adjudicate": "gemini-3.5-flash-lite",
        "plan": "gemini-3.5-flash",
        "synthesize": "gemini-3.5-flash",
        "judge": "gemini-3.5-flash-lite",
    },
    "ollama": dict.fromkeys(
        ("extract", "caption", "adjudicate", "plan", "synthesize", "judge"), "gemma3:4b"
    ),
    "fake": dict.fromkeys(
        ("extract", "caption", "adjudicate", "plan", "synthesize", "judge"), "fake"
    ),
}

# Free-tier quotas are per model and small (a busy day can exhaust one), so each task has a
# fallback chain. A model that answers 429 is skipped for a cooldown (see ModelCooldowns).
GEMINI_FALLBACKS: dict[Task, tuple[str, ...]] = {
    "extract": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "caption": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "adjudicate": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "plan": ("gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
    "synthesize": ("gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
    # Eval judge (Week 5): cheap model, separate quota from the agent where possible.
    "judge": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
}

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# The agent's own tasks. A run makes a handful of calls, far under any per-minute quota, so they
# skip the fixed pacing interval (a 429 still falls over along the chain).
AGENT_TASKS: frozenset[Task] = frozenset({"plan", "synthesize"})

# ESTIMATE ONLY, for reporting: assumed paid-tier list prices in USD per million tokens
# (input, output incl. thinking), matched by the longest model-name prefix. The free tier bills
# nothing, so agent_runs.cost_usd is 0 there; see the README section "Usage and cost".
LIST_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3-flash": (0.50, 3.00),
}


def list_price(model: str) -> tuple[float, float] | None:
    best = max((p for p in LIST_PRICES_USD_PER_MTOK if model.startswith(p)), key=len, default=None)
    return None if best is None else LIST_PRICES_USD_PER_MTOK[best]


@dataclass(frozen=True)
class CallUsage:
    """One model reply (tokens as the provider reported them, else 0)."""

    task: Task
    model: str
    ms: int
    input_tokens: int = 0
    output_tokens: int = 0  # answer + thinking tokens
    thought_tokens: int = 0


@dataclass
class UsageMeter:
    """The model calls of one agent run. Thread-safe; use one per run."""

    calls: list[CallUsage] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: CallUsage) -> None:
        with self._lock:
            self.calls.append(usage)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    def list_price_usd(self) -> float | None:
        """Estimated cost at LIST_PRICES_USD_PER_MTOK; None if a model has no known price."""
        total = 0.0
        for c in self.calls:
            price = list_price(c.model)
            if price is None:
                return None
            total += (c.input_tokens * price[0] + c.output_tokens * price[1]) / 1_000_000
        return round(total, 6)

    def summary(self) -> dict[str, Any]:
        return {
            "model_calls": len(self.calls),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "list_price_usd_estimate": self.list_price_usd(),
            "calls": [
                {
                    "task": c.task,
                    "model": c.model,
                    "ms": c.ms,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "thought_tokens": c.thought_tokens,
                }
                for c in self.calls
            ],
        }


class RouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CASELENS_", extra="ignore")

    llm_provider: Provider = "gemini"
    gemini_api_key: SecretStr | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    # Per-task model overrides; None uses DEFAULT_MODELS for the provider.
    model_extract: str | None = None
    model_caption: str | None = None
    model_adjudicate: str | None = None
    model_plan: str | None = None
    model_synthesize: str | None = None
    model_judge: str | None = None
    llm_threads: int = 4  # ollama CPU threads, kept low so the machine stays responsive
    llm_timeout_s: float = 120.0
    llm_context_tokens: int = 8192
    # Free-tier pacing: minimum seconds between hosted calls, and retries on HTTP 429 / 5xx.
    llm_min_interval_s: float = 4.0  # per model, for extract / caption / adjudicate / judge
    # Plan / synthesize: a run is a few calls, well under the free-tier per-minute quota.
    llm_agent_min_interval_s: float = 0.0
    # ... but a router shared by many runs (the eval harness) keeps each agent model under this
    # many requests per rolling minute, so a burst never turns into 429s (0 = no limit).
    llm_agent_rpm: int = 10
    # The free tier bills nothing: agent_runs.cost_usd is 0 (a list-price estimate goes in plan).
    llm_free_tier: bool = True
    # Gemini thinking: "minimal" keeps extraction / planning steps around 1.5 s; synthesis gets a
    # little more room. None leaves the model default.
    gemini_thinking_level: str | None = "minimal"
    gemini_synthesize_thinking_level: str | None = "low"
    llm_rate_limit_retries: int = 5
    # On 429 / repeated 5xx, move on to the task's next model instead of waiting.
    gemini_fallback: bool = True
    llm_overload_retries: int = 2
    # A model that answered 429 is skipped by every router in the process for the server's
    # retryDelay, else this long (a per-day quota: `llm_quota_day_cooldown_s`).
    llm_quota_cooldown_s: float = 60.0
    llm_quota_day_cooldown_s: float = 3600.0
    # Plan / synthesize calls that take longer than this fail over to the next model.
    llm_agent_timeout_s: float = 30.0


class ModelError(RuntimeError):
    """The model call failed, or did not return valid JSON for the schema after one retry."""


class _UnavailableError(ModelError):
    """This model can't serve the call right now (quota or overload); another one may."""

    def __init__(self, message: str, quota: bool, cooldown_s: float = 0.0) -> None:
        super().__init__(message)
        self.quota = quota
        self.cooldown_s = cooldown_s


class ModelCooldowns:
    """Models that answered 429, shared by the routers of one process (the API makes a router
    per request), so a spent free-tier quota is not re-probed on every question."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, model: str, seconds: float) -> None:
        with self._lock:
            until = self._clock() + seconds
            self._until[model] = max(until, self._until.get(model, 0.0))

    def cooling(self, model: str) -> bool:
        with self._lock:
            return self._until.get(model, 0.0) > self._clock()

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


SHARED_COOLDOWNS = ModelCooldowns()

_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


def quota_cooldown(response: httpx.Response, default_s: float, day_s: float) -> float:
    """How long to skip a model after this 429, from Gemini's RetryInfo / QuotaFailure details
    (only those fields are read; nothing from the body is logged or raised)."""
    try:
        data = response.json()
    except ValueError:
        return default_s
    error = data.get("error") if isinstance(data, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    delay: float | None = None
    for item in details if isinstance(details, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            violations = item.get("violations")
            for v in violations if isinstance(violations, list) else []:
                if isinstance(v, dict) and "PerDay" in str(v.get("quotaId", "")):
                    return day_s
        if kind.endswith("RetryInfo"):
            match = _DELAY.match(str(item.get("retryDelay", "")))
            if match:
                delay = float(match.group(1))
    return default_s if delay is None else min(max(delay, 1.0), day_s)


def evidence_block(label: str, text: str) -> str:
    """Wrap evidence text so the model treats it as quoted data."""
    safe = text.replace("<<<", "< < <").replace(">>>", "> > >")
    return f"<<<EVIDENCE {label}\n{safe}\n>>>"


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    raise ModelError("images must be PNG or JPEG")


FakeResponder = Callable[[Task, str, str, type[BaseModel]], str]


class ModelRouter:
    def __init__(
        self,
        settings: RouterSettings | None = None,
        fake: FakeResponder | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cooldowns: ModelCooldowns | None = None,
    ) -> None:
        self.settings = settings or RouterSettings()
        # Routers on the real endpoint share the process table; an injected client (a test
        # transport) gets its own unless one is passed.
        if cooldowns is None:
            cooldowns = SHARED_COOLDOWNS if client is None else ModelCooldowns()
        self.cooldowns = cooldowns
        self._fake = fake
        self._client = client
        self._sleep = sleep
        self._last_call: dict[str, float] = {}
        self._recent: dict[str, deque[float]] = {}  # agent-task call times per URL (RPM window)
        self.last_model: dict[Task, str] = {}

    def model_for(self, task: Task) -> str:
        override: str | None = getattr(self.settings, f"model_{task}")
        return override or DEFAULT_MODELS[self.settings.llm_provider][task]

    def complete_json(
        self,
        task: Task,
        system: str,
        user: str,
        schema: type[T],
        images: Sequence[bytes] = (),
        meter: UsageMeter | None = None,
    ) -> T:
        """`meter`, if given, gets one CallUsage per model reply (tokens and latency)."""
        last_error = ""
        for _ in range(2):
            prompt = (
                user
                if not last_error
                else f"{user}\n\nYour previous reply was invalid: {last_error}"
            )
            start = time.monotonic()
            raw, model, tokens = self._call(task, system, prompt, schema, images)
            if meter is not None:
                ms = int((time.monotonic() - start) * 1000)
                meter.add(CallUsage(task, model, ms, *tokens))
            try:
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)[:500]
        raise ModelError(f"{task}: invalid JSON from {self.model_for(task)}: {last_error}")

    def _call(
        self,
        task: Task,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: Sequence[bytes],
    ) -> tuple[str, str, tuple[int, int, int]]:
        """(raw reply, model that answered, (input, output, thought) tokens)."""
        provider = self.settings.llm_provider
        if provider == "fake":
            if self._fake is None:
                raise ModelError("fake provider selected but no responder given")
            return self._fake(task, system, user, schema), "fake", (0, 0, 0)
        if provider == "gemini":
            return self._gemini(task, system, user, schema, images)
        return self._ollama(task, system, user, schema, images)

    def _post(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        fail_over: bool = False,
        min_interval_s: float | None = None,
        rpm: int = 0,
        timeout_s: float | None = None,
    ) -> Any:
        """POST with pacing. With `fail_over`, a 429 or persistent 5xx raises `_UnavailableError` at
        once so the caller can try another model; otherwise both are retried with backoff."""
        s = self.settings
        interval = s.llm_min_interval_s if min_interval_s is None else min_interval_s
        retries = s.llm_overload_retries if fail_over else s.llm_rate_limit_retries
        client = self._client or httpx.Client(timeout=s.llm_timeout_s)
        try:
            for attempt in range(retries + 1):
                wait = self._last_call.get(url, 0.0) + interval - time.monotonic()
                recent = self._recent.setdefault(url, deque())
                now = time.monotonic()
                while recent and recent[0] <= now - 60.0:
                    recent.popleft()
                if rpm > 0 and len(recent) >= rpm:
                    wait = max(wait, recent[-rpm] + 60.0 - now)
                if wait > 0 and self.settings.llm_provider == "gemini":
                    self._sleep(wait)
                self._last_call[url] = time.monotonic()
                recent.append(self._last_call[url])
                try:
                    if timeout_s is None:
                        response = client.post(url, json=body, headers=headers)
                    else:
                        response = client.post(url, json=body, headers=headers, timeout=timeout_s)
                except httpx.TimeoutException as exc:
                    if fail_over:
                        raise _UnavailableError("model request timed out", quota=False) from exc
                    raise ModelError(f"model request failed: {type(exc).__name__}") from exc
                except httpx.HTTPError as exc:
                    raise ModelError(f"model request failed: {type(exc).__name__}") from exc
                code = response.status_code
                if code == 429 and fail_over:
                    cooldown = quota_cooldown(
                        response, s.llm_quota_cooldown_s, s.llm_quota_day_cooldown_s
                    )
                    raise _UnavailableError(
                        "model request failed: HTTP 429", quota=True, cooldown_s=cooldown
                    )
                if (code == 429 or code >= 500) and attempt < retries:
                    # 429 = quota: back off hard. 5xx = transient overload: retry quickly.
                    self._sleep(min(60.0, (5.0 if code == 429 else 1.0) * 2**attempt))
                    continue
                if code >= 500 and fail_over:
                    raise _UnavailableError(f"model request failed: HTTP {code}", quota=False)
                if code >= 400:
                    # Never echo request bodies or headers (they carry the key / evidence).
                    raise ModelError(f"model request failed: HTTP {code}")
                return response.json()
            raise ModelError("model request failed: retries exhausted")
        finally:
            if self._client is None:
                client.close()

    def _gemini(
        self,
        task: Task,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: Sequence[bytes],
    ) -> tuple[str, str, tuple[int, int, int]]:
        key = self.settings.gemini_api_key
        if key is None:
            raise ModelError("CASELENS_GEMINI_API_KEY is not set")
        parts: list[dict[str, Any]] = [{"text": user}]
        parts += [
            {"inline_data": {"mime_type": _image_mime(img), "data": base64.b64encode(img).decode()}}
            for img in images
        ]
        generation: dict[str, Any] = {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema.model_json_schema(),
        }
        level = (
            self.settings.gemini_synthesize_thinking_level
            if task == "synthesize"
            else self.settings.gemini_thinking_level
        )
        if level:
            generation["thinkingConfig"] = {"thinkingLevel": level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation,
        }
        chain = [self.model_for(task)]
        if self.settings.gemini_fallback:
            chain += [m for m in GEMINI_FALLBACKS[task] if m not in chain]
        candidates = [m for m in chain if not self.cooldowns.cooling(m)] or chain[-1:]
        agent = task in AGENT_TASKS
        interval = self.settings.llm_agent_min_interval_s if agent else None
        rpm = self.settings.llm_agent_rpm if agent else 0
        data: Any = None
        answered = candidates[-1]
        for i, model in enumerate(candidates):
            last = i == len(candidates) - 1
            try:
                data = self._post(
                    GEMINI_URL.format(model=model),
                    body,
                    {"x-goog-api-key": key.get_secret_value()},
                    fail_over=not last,
                    min_interval_s=interval,
                    rpm=rpm,
                    timeout_s=self.settings.llm_agent_timeout_s if agent else None,
                )
            except _UnavailableError as exc:
                if exc.quota:
                    self.cooldowns.mark(model, exc.cooldown_s)
                continue
            self.last_model[task] = model
            answered = model
            break
        if data is None:
            raise ModelError("model request failed: every model in the chain is unavailable")
        try:
            parts_out = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("gemini returned no content") from exc
        # Thinking models may add thought parts; keep only the answer text.
        text = "".join(
            p["text"] for p in parts_out if isinstance(p.get("text"), str) and not p.get("thought")
        )
        if not text:
            raise ModelError("gemini returned no content")
        meta = data.get("usageMetadata") if isinstance(data, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        thoughts = _count(meta.get("thoughtsTokenCount"))
        tokens = (
            _count(meta.get("promptTokenCount")),
            _count(meta.get("candidatesTokenCount")) + thoughts,
            thoughts,
        )
        return text, answered, tokens

    def _ollama(
        self,
        task: Task,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: Sequence[bytes],
    ) -> tuple[str, str, tuple[int, int, int]]:
        s = self.settings
        message: dict[str, object] = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        body = {
            "model": self.model_for(task),
            "messages": [{"role": "system", "content": system}, message],
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_thread": s.llm_threads,
                "num_ctx": s.llm_context_tokens,
            },
        }
        data = self._post(f"{s.ollama_url}/api/chat", body, {})
        content = data["message"]["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        tokens = (_count(data.get("prompt_eval_count")), _count(data.get("eval_count")), 0)
        return text, self.model_for(task), tokens


def _count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
