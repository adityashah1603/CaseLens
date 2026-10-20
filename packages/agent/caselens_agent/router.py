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
    llm_min_interval_s: float = 4.0
    llm_rate_limit_retries: int = 5


class ModelError(RuntimeError):
    """The model call failed, or did not return valid JSON for the schema after one retry."""


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
        self._last_call = 0.0

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

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        s = self.settings
        client = self._client or httpx.Client(timeout=s.llm_timeout_s)
        try:
            for attempt in range(s.llm_rate_limit_retries + 1):
                wait = self._last_call + s.llm_min_interval_s - time.monotonic()
                if wait > 0 and self.settings.llm_provider == "gemini":
                    self._sleep(wait)
                self._last_call = time.monotonic()
                try:
                    response = client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    raise ModelError(f"model request failed: {type(exc).__name__}") from exc
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < s.llm_rate_limit_retries:
                    self._sleep(min(60.0, 5.0 * 2**attempt))
                    continue
                if response.status_code >= 400:
                    # Never echo request bodies or headers (they carry the key / evidence).
                    raise ModelError(f"model request failed: HTTP {response.status_code}")
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
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema.model_json_schema(),
            },
        }
        data = self._post(
            GEMINI_URL.format(model=self.model_for(task)),
            body,
            {"x-goog-api-key": key.get_secret_value()},
        )
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
