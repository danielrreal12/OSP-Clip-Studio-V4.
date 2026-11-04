from types import SimpleNamespace

import pytest

from osp.ai.openai_provider import OpenAIProvider
from osp.ai.vision import normalize_vision
from osp.config import Settings
from osp.errors import ConfigurationError, ProviderError


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(
            content='{"frames":[{"timestamp":1.25,"summary":"A pole","objects":["pole"],"activity":"standing","osp_elements":["utility_pole"],"osp_relevance":0.7,"daylight":true,"quality_notes":"","sensitive":[],"needs_closer_look":false}]}'
        )
        usage = SimpleNamespace(prompt_tokens=33, completion_tokens=9)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_missing_key_is_a_configuration_error():
    with pytest.raises(ConfigurationError):
        OpenAIProvider(Settings(openai_api_key="", ai_provider="openai"))


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigurationError):
        OpenAIProvider(Settings(openai_api_key="sk-test", ai_provider="local-mock"))


def test_vision_request_sends_image_bytes_and_reads_usage():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = OpenAIProvider(Settings(openai_api_key="sk-test", ai_provider="openai"), client=client)
    parsed, usage = provider.complete_json(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "look"}],
        images=[{"bytes": b"\xff\xd8\xff\xd9", "mime": "image/jpeg"}],
        detail="low",
    )
    content = completions.kwargs["messages"][1]["content"]
    assert any(block.get("type") == "image_url" for block in content)
    assert "base64" in content[1]["image_url"]["url"]
    assert parsed["frames"][0]["timestamp"] == 1.25
    assert usage == {"prompt_tokens": 33, "completion_tokens": 9}


def test_vision_keeps_notes_when_the_model_numbers_frames_from_zero():
    payload = {
        "frames": [
            {
                "timestamp": 0,
                "summary": "A utility pole",
                "objects": ["pole"],
                "osp_elements": ["utility_pole"],
                "osp_relevance": 0.8,
                "daylight": True,
                "activity": "standing",
            },
            {
                "timestamp": 1,
                "summary": "A bucket truck",
                "objects": ["truck"],
                "osp_elements": ["bucket_truck"],
                "osp_relevance": 0.9,
                "daylight": True,
                "activity": "raised",
            },
        ]
    }
    notes = normalize_vision(payload, [14.2, 30.5])
    assert [note["timestamp"] for note in notes] == [14.2, 30.5]
    assert "pole" in notes[0]["summary"]
    assert "truck" in notes[1]["summary"]


def test_vision_still_uses_real_timestamps_when_the_model_copies_them():
    payload = {
        "frames": [
            {"timestamp": 30.5, "summary": "A bucket truck"},
            {"timestamp": 14.2, "summary": "A utility pole"},
        ]
    }
    notes = normalize_vision(payload, [14.2, 30.5])
    assert notes[0]["timestamp"] == 14.2
    assert "pole" in notes[0]["summary"]
    assert notes[1]["timestamp"] == 30.5


def test_rejected_vision_request_includes_the_reason():
    import httpx
    from openai import BadRequestError

    class Boom:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise BadRequestError("bad", response=response, body={"error": {"message": "Invalid image data."}})

    client = SimpleNamespace(chat=SimpleNamespace(completions=Boom()))
    provider = OpenAIProvider(Settings(openai_api_key="sk-test", ai_provider="openai"), client=client)
    with pytest.raises(ProviderError) as caught:
        provider.complete_json(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "look"}],
            images=[{"bytes": b"\xff\xd8\xff", "mime": "image/jpeg"}],
        )
    assert "Invalid image data" in str(caught.value)
