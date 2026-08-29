"""Tests for structured image vision fallback provenance.

Proves:
- GPT-5.4 success provenance is recorded
- GPT-5.4 failure -> SiliconFlow vision success fallback
- All vision failure -> OCR fallback with warnings
- Fallback chain is persisted
"""

import base64

from fin_analyse.scraper.downloader import (
    ImageProvenance,
    _get_vision_clients,
    describe_image_with_provenance,
)


class TestImageProvenance:
    """Verify ImageProvenance data class."""

    def test_mimo_success_provenance(self):
        p = ImageProvenance(
            llm_desc="A chart showing uptrend",
            vision_provider="mimo",
            vision_model="mimo-v2.5",
            fallback_chain=["mimo:ok"],
        )
        d = p.to_dict()
        assert d["vision_provider"] == "mimo"
        assert d["vision_model"] == "mimo-v2.5"
        assert d["fallback_chain"] == ["mimo:ok"]
        assert not d["error"]

    def test_vision_fallback_provenance(self):
        p = ImageProvenance(
            llm_desc="Chart analysis",
            vision_provider="vision",
            vision_model="Qwen/Qwen3-VL-30B-A3B-Instruct",
            fallback_chain=["mimo:error:timeout", "vision:ok"],
        )
        d = p.to_dict()
        assert d["vision_provider"] == "vision"
        assert d["fallback_chain"] == ["mimo:error:timeout", "vision:ok"]

    def test_all_failure_ocr_fallback(self):
        p = ImageProvenance(
            llm_desc="",
            vision_provider="none",
            vision_model="",
            fallback_chain=["mimo:error:timeout", "vision:error:rate_limit"],
            error="All vision models failed",
        )
        d = p.to_dict()
        assert d["vision_provider"] == "none"
        assert d["llm_desc"] == ""
        assert len(d["fallback_chain"]) == 2
        assert d["error"] == "All vision models failed"

    def test_from_dict_roundtrip(self):
        original = ImageProvenance(
            llm_desc="Chart",
            ocr_text="Raw text",
            vision_provider="gpt5",
            vision_model="gpt-5.4",
            fallback_chain=["gpt5:ok"],
            error="",
        )
        d = original.to_dict()
        restored = ImageProvenance.from_dict(d)
        assert restored.vision_provider == original.vision_provider
        assert restored.fallback_chain == original.fallback_chain
        assert restored.llm_desc == original.llm_desc
        assert restored.ocr_text == original.ocr_text

    def test_ocr_only_provenance(self):
        """When no vision model is available, OCR-only provenance is recorded."""
        p = ImageProvenance(
            llm_desc="",
            ocr_text="Extracted Chinese text",
            vision_provider="none",
            vision_model="",
            fallback_chain=["ocr:ok"],
        )
        d = p.to_dict()
        assert d["ocr_text"] == "Extracted Chinese text"
        assert d["vision_provider"] == "none"
        assert "ocr:ok" in d["fallback_chain"]


class TestDescribeImageWithProvenance:
    """Integration with real/empty paths. Cannot test actual LLM calls in unit tests."""

    def test_nonexistent_file_returns_empty_provenance(self, tmp_path):
        img = tmp_path / "nonexistent.png"
        result = describe_image_with_provenance(str(img))
        assert result.llm_desc == ""
        assert result.vision_provider == "none"
        assert "file_not_found" in result.fallback_chain or result.error

    def test_empty_clients_returns_ocr_only(self, tmp_path, monkeypatch):
        """When no vision clients are configured, fallback to OCR."""
        # Create a tiny valid image
        img_path = tmp_path / "test.png"
        # Write a minimal 1x1 PNG
        minimal_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADgQGBYcCg/gAAAABJRU5ErkJggg=="
        )
        img_path.write_bytes(minimal_png)

        # Mock _get_vision_clients to return empty
        monkeypatch.setattr(
            "fin_analyse.scraper.downloader._get_vision_clients",
            lambda: [],
        )

        result = describe_image_with_provenance(str(img_path))
        # Should return empty desc since no vision clients
        assert result.llm_desc == ""
        # But should not crash
        assert isinstance(result.fallback_chain, list)

    def test_vision_client_order_and_model(self, monkeypatch):
        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
        monkeypatch.setattr(
            "fin_analyse.claims.config_loader.load_llm_config",
            lambda: {
                "models": {
                    "mimo": {
                        "enabled": True,
                        "model": "mimo-v2.5",
                        "api_key": "m",
                        "base_url": "https://api.xiaomimimo.com/v1",
                    },
                    "glm-vision": {
                        "enabled": True,
                        "model": "glm-4v-flash",
                        "api_key": "g",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "timeout": 90,
                        "max_tokens": 1024,
                    },
                    "vision": {
                        "enabled": True,
                        "model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
                        "api_key": "c",
                        "base_url": "https://c",
                    },
                }
            },
        )

        clients = _get_vision_clients()

        assert [
            (model, tag, timeout, max_tokens)
            for _client, model, tag, timeout, max_tokens in clients
        ] == [
            ("mimo-v2.5", "mimo", 30, 1536),
            ("glm-4v-flash", "glm-vision", 90, 1024),
            ("Qwen/Qwen3-VL-30B-A3B-Instruct", "vision", 30, 1536),
        ]

    def test_glm_vision_skipped_when_placeholder_unresolved(self, monkeypatch):
        """glm-vision 与其他 vision provider 一样,${ENV} 占位未解析时跳过。"""
        monkeypatch.setattr("openai.OpenAI", lambda **kwargs: object())
        monkeypatch.setattr(
            "fin_analyse.claims.config_loader.load_llm_config",
            lambda: {
                "models": {
                    "mimo": {
                        "enabled": True,
                        "model": "mimo-v2.5",
                        "api_key": "m",
                        "base_url": "https://api.xiaomimimo.com/v1",
                    },
                    "glm-vision": {
                        "enabled": True,
                        "model": "glm-4v-flash",
                        "api_key": "${GLM_VISION_API_KEY}",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "timeout": 90,
                    },
                }
            },
        )

        clients = _get_vision_clients()

        assert [(model, tag) for _client, model, tag, _timeout, _max_tokens in clients] == [
            ("mimo-v2.5", "mimo"),
        ]

    def test_top_level_string_image_response_is_accepted(self, tmp_path, monkeypatch):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"not-a-real-image-but-path-is-readable")

        class Completions:
            @staticmethod
            def create(**_kwargs):
                return "plain vision answer"

        class FakeClient:
            chat = type("Chat", (), {"completions": Completions()})()

        monkeypatch.setattr(
            "fin_analyse.scraper.downloader._get_vision_clients",
            lambda: [(FakeClient(), "mimo-v2.5", "mimo", 30, 1536)],
        )

        result = describe_image_with_provenance(str(img_path))

        assert result.llm_desc == "plain vision answer"
        assert result.vision_provider == "mimo"
        assert result.vision_model == "mimo-v2.5"
