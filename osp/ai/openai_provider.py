"""OpenAI vision, director, and speech calls. There is no offline stand-in."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

from osp.errors import ConfigurationError, ProviderError

log = logging.getLogger("osp.ai.openai")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class OpenAIProvider:
    def __init__(self, settings, client=None):
        if settings.ai_provider != "openai":
            raise ConfigurationError(
                f"AI_PROVIDER={settings.ai_provider} is not supported. Set AI_PROVIDER=openai."
            )
        if not settings.openai_api_key.strip():
            raise ConfigurationError(
                "OPENAI_API_KEY is not configured. Autonomous editing did not run."
            )
        self.settings = settings
        self._client = client

    def check_access(self) -> None:
        """Reject a bad key before the slow measurement pass starts."""
        from openai import APIConnectionError, APIStatusError, AuthenticationError

        try:
            self.client().models.retrieve(self.settings.vision_model)
        except AuthenticationError as exc:
            raise ConfigurationError("OPENAI_API_KEY was rejected. Check the key on the server.") from exc
        except APIStatusError as exc:
            if exc.status_code in {401, 403}:
                raise ConfigurationError("OPENAI_API_KEY was rejected. Check the key on the server.") from exc
            log.warning("Key check returned HTTP %s; the edit will continue", exc.status_code)
        except APIConnectionError as exc:
            raise ProviderError("Could not reach the AI provider to check the key.") from exc

    def client(self):
        if self._client is not None:
            return self._client
        from openai import OpenAI

        kwargs = {"api_key": self.settings.openai_api_key, "timeout": self.settings.ai_timeout_sec}
        if self.settings.ai_base_url:
            kwargs["base_url"] = self.settings.ai_base_url
        self._client = OpenAI(**kwargs)
        return self._client

    def complete_json(self, *, model: str, messages: list, images: list | None = None, detail: str = "low") -> tuple[dict, dict]:
        payload = _with_images(messages, images or [], detail)
        response = self._chat(model, payload)
        content = response.choices[0].message.content or ""
        try:
            parsed = json.loads(_FENCE.sub("", content.strip()))
        except json.JSONDecodeError as exc:
            raise ProviderError("The AI service returned an unreadable response. " + _detail(content)) from exc
        if not isinstance(parsed, dict):
            raise ProviderError("The AI service returned a response that was not a JSON object.")
        usage = getattr(response, "usage", None)
        return parsed, {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }

    def transcribe(self, path: Path, *, model: str) -> tuple[dict, dict]:
        result, word_level = self._transcribe_call(path, model)
        words = []
        for item in _list_attr(result, "words"):
            data = _as_dict(item)
            text = str(data.get("word") or data.get("text") or "").strip()
            if not text:
                continue
            words.append(
                {
                    "word": text,
                    "start": float(data.get("start") or 0),
                    "end": float(data.get("end") or 0),
                }
            )
        segments = []
        for item in _list_attr(result, "segments"):
            data = _as_dict(item)
            segments.append(
                {
                    "start": float(data.get("start") or 0),
                    "end": float(data.get("end") or 0),
                    "text": str(data.get("text") or "").strip(),
                }
            )
        text = str(getattr(result, "text", "") or "").strip()
        if not text and segments:
            text = " ".join(segment["text"] for segment in segments).strip()
        return {
            "text": text,
            "words": words,
            "segments": segments,
            "word_timestamps": bool(word_level and words),
        }, {}

    def _chat(self, model: str, messages: list):
        from openai import APIConnectionError, APIStatusError, AuthenticationError, BadRequestError, RateLimitError

        def call():
            return self.client().chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=messages,
            )

        try:
            return _with_retry(call)
        except AuthenticationError as exc:
            raise ConfigurationError("OPENAI_API_KEY was rejected. Check the key on the server.") from exc
        except RateLimitError as exc:
            raise ProviderError("The AI provider rate limit or quota was hit. Nothing was exported.") from exc
        except BadRequestError as exc:
            raise ProviderError("The AI request was rejected. " + _error_detail(exc)) from exc
        except APIConnectionError as exc:
            raise ProviderError("The AI provider could not be reached. " + _error_detail(exc)) from exc
        except APIStatusError as exc:
            raise ProviderError(f"The AI provider returned HTTP {exc.status_code}. " + _error_detail(exc)) from exc

    def _transcribe_call(self, path: Path, model: str):
        from openai import APIConnectionError, APIStatusError, AuthenticationError, BadRequestError, RateLimitError

        def attempt(granular: bool):
            with path.open("rb") as handle:
                kwargs = {"model": model, "file": handle, "response_format": "verbose_json"}
                if granular:
                    kwargs["timestamp_granularities"] = ["word", "segment"]
                return self.client().audio.transcriptions.create(**kwargs)

        try:
            return _with_retry(lambda: attempt(True)), True
        except BadRequestError:
            log.info("Word timestamps were rejected; retrying segment transcription")
            try:
                return _with_retry(lambda: attempt(False)), False
            except BadRequestError as exc:
                raise ProviderError("Transcription was rejected by the provider.") from exc
        except AuthenticationError as exc:
            raise ConfigurationError("OPENAI_API_KEY was rejected. Check the key on the server.") from exc
        except RateLimitError as exc:
            raise ProviderError("Transcription hit a rate limit or quota. Nothing was exported.") from exc
        except APIConnectionError as exc:
            raise ProviderError("Transcription could not reach the AI provider.") from exc
        except APIStatusError as exc:
            raise ProviderError(f"Transcription failed with HTTP {exc.status_code}.") from exc


def _error_detail(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif error:
            message = str(error)
    if not message:
        message = str(exc)
    return _detail(message)


def _detail(text: str) -> str:
    from osp.errors import scrub

    return scrub(text, 220)


def build_provider(settings, client=None) -> OpenAIProvider:
    return OpenAIProvider(settings, client=client)


def _with_retry(fn):
    from openai import APIStatusError, RateLimitError

    delay = 1.4
    last = None
    for attempt in range(2):
        try:
            return fn()
        except RateLimitError as exc:
            last = exc
            if attempt == 1:
                raise
            time.sleep(delay)
        except APIStatusError as exc:
            last = exc
            if exc.status_code < 500 or attempt == 1:
                raise
            time.sleep(delay)
    raise last


def _with_images(messages: list, images: list, detail: str) -> list:
    if not images:
        return messages
    copied = [dict(message) for message in messages]
    # Attach stills to the last user turn.
    for index in range(len(copied) - 1, -1, -1):
        if copied[index].get("role") == "user":
            content = copied[index].get("content")
            blocks = [{"type": "text", "text": content if isinstance(content, str) else json.dumps(content)}]
            for image in images:
                encoded = base64.b64encode(image["bytes"]).decode("ascii")
                mime = image.get("mime") or "image/jpeg"
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": detail},
                    }
                )
            copied[index]["content"] = blocks
            break
    return copied


def _as_dict(item) -> dict:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return {
        "word": getattr(item, "word", ""),
        "text": getattr(item, "text", ""),
        "start": getattr(item, "start", 0),
        "end": getattr(item, "end", 0),
    }


def _list_attr(result, name: str) -> list:
    value = getattr(result, name, None)
    if value is None and isinstance(result, dict):
        value = result.get(name)
    return list(value or [])
