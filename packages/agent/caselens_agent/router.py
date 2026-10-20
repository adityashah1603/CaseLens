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
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

Task = Literal["extract", "caption", "adjudicate", "plan", "synthesize"]
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
    },
    "ollama": dict.fromkeys(
        ("extract", "caption", "adjudicate", "plan", "synthesize"), "gemma3:4b"
    ),
    "fake": dict.fromkeys(("extract", "caption", "adjudicate", "plan", "synthesize"), "fake"),
}

# Free-tier quotas are per model and small (a busy day can exhaust one), so each task has a
# fallback chain. A model that answers 429 is skipped for the rest of the process.
GEMINI_FALLBACKS: dict[Task, tuple[str, ...]] = {
    "extract": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "caption": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "adjudicate": ("gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "plan": ("gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
    "synthesize": ("gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
}

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


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
    llm_threads: int = 4  # ollama CPU threads, kept low so the machine stays responsive
    llm_timeout_s: float = 120.0
    llm_context_tokens: int = 8192
    # Free-tier pacing: minimum seconds between hosted calls, and retries on HTTP 429 / 5xx.
    llm_min_interval_s: float = 4.0  # per model
    # Gemini thinking: "minimal" keeps extraction / planning steps around 1.5 s; synthesis gets a
    # little more room. None leaves the model default.
    gemini_thinking_level: str | None = "minimal"
    gemini_synthesize_thinking_level: str | None = "low"
    llm_rate_limit_retries: int = 5
    # On 429 / repeated 5xx, move on to the task's next model instead of waiting.
    gemini_fallback: bool = True
    llm_overload_retries: int = 2


class ModelError(RuntimeError):
    """The model call failed, or did not return valid JSON for the schema after one retry."""


class _UnavailableError(ModelError):
    """This model can't serve the call right now (quota or overload); another one may."""

    def __init__(self, message: str, quota: bool) -> None:
        super().__init__(message)
        self.quota = quota


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
    ) -> None:
        self.settings = settings or RouterSettings()
        self._fake = fake
        self._client = client
        self._sleep = sleep
        self._last_call: dict[str, float] = {}
        self._exhausted: set[str] = set()
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
    ) -> T:
        last_error = ""
        for _ in range(2):
            prompt = (
                user
                if not last_error
                else f"{user}\n\nYour previous reply was invalid: {last_error}"
            )
            raw = self._call(task, system, prompt, schema, images)
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
    ) -> str:
        provider = self.settings.llm_provider
        if provider == "fake":
            if self._fake is None:
                raise ModelError("fake provider selected but no responder given")
            return self._fake(task, system, user, schema)
        if provider == "gemini":
            return self._gemini(task, system, user, schema, images)
        return self._ollama(task, system, user, schema, images)

    def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], fail_over: bool = False
    ) -> Any:
        """POST with pacing. With `fail_over`, a 429 or persistent 5xx raises `_UnavailableError` at
        once so the caller can try another model; otherwise both are retried with backoff."""
        s = self.settings
        retries = s.llm_overload_retries if fail_over else s.llm_rate_limit_retries
        client = self._client or httpx.Client(timeout=s.llm_timeout_s)
        try:
            for attempt in range(retries + 1):
                wait = self._last_call.get(url, 0.0) + s.llm_min_interval_s - time.monotonic()
                if wait > 0 and self.settings.llm_provider == "gemini":
                    self._sleep(wait)
                self._last_call[url] = time.monotonic()
                try:
                    response = client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    raise ModelError(f"model request failed: {type(exc).__name__}") from exc
                code = response.status_code
                if code == 429 and fail_over:
                    raise _UnavailableError("model request failed: HTTP 429", quota=True)
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
    ) -> str:
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
        candidates = [m for m in chain if m not in self._exhausted] or chain[-1:]
        data: Any = None
        for i, model in enumerate(candidates):
            last = i == len(candidates) - 1
            try:
                data = self._post(
                    GEMINI_URL.format(model=model),
                    body,
                    {"x-goog-api-key": key.get_secret_value()},
                    fail_over=not last,
                )
            except _UnavailableError as exc:
                if exc.quota:
                    self._exhausted.add(model)
                continue
            self.last_model[task] = model
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
        return text

    def _ollama(
        self,
        task: Task,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: Sequence[bytes],
    ) -> str:
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
        return content if isinstance(content, str) else json.dumps(content)
