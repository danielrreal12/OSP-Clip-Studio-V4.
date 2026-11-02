from types import SimpleNamespace

import pytest

from osp.ai.openai_provider import OpenAIProvider
from osp.config import Settings
from osp.errors import ConfigurationError


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
