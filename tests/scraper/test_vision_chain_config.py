"""vision.chain 配置化：解析分层、客户端装配顺序、熔断键 vision: 前缀。"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fin_analyse.scraper import downloader


def _cfg(models: dict | None = None, vision: dict | None = None) -> dict:
    return {"models": models or {}, "vision": vision or {}}


class FakeBreaker:
    def __init__(self, open_names: frozenset[str] = frozenset()):
        self.open_names = open_names
        self.checked: list[str] = []
        self.failed: list[str] = []
        self.succeeded: list[str] = []

    def can_try(self, name: str) -> bool:
        self.checked.append(name)
        return name not in self.open_names

    def record_failure(self, name: str, reason: object = "") -> None:
        self.failed.append(name)

    def record_success(self, name: str) -> None:
        self.succeeded.append(name)


def _patch_config(monkeypatch, cfg: dict, breaker: FakeBreaker | None = None) -> None:
    import fin_analyse.claims.backend_health as bh
    import fin_analyse.claims.config_loader as cl

    monkeypatch.setattr(cl, "load_llm_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bh, "get_backend_circuit_breaker", lambda: breaker)


class TestVisionChainParsing:
    def test_missing_section_falls_back_to_current_default(self):
        assert downloader._vision_chain({}) == ["mimo", "glm-vision", "vision"]

    def test_missing_chain_key_falls_back(self):
        assert downloader._vision_chain({"vision": {}}) == ["mimo", "glm-vision", "vision"]

    def test_non_list_chain_falls_back(self):
        cfg = _cfg(vision={"chain": "mimo"})
        assert downloader._vision_chain(cfg) == ["mimo", "glm-vision", "vision"]

    def test_explicit_chain_kept_in_order(self):
        cfg = _cfg(vision={"chain": ["glm53_flash", "glm-vision", "vision", "mimo"]})
        assert downloader._vision_chain(cfg) == ["glm53_flash", "glm-vision", "vision", "mimo"]

    def test_empty_chain_is_legal_explicit_config(self):
        assert downloader._vision_chain(_cfg(vision={"chain": []})) == []

    def test_dedup_preserves_first_occurrence(self):
        cfg = _cfg(vision={"chain": ["vision", "mimo", "vision"]})
        assert downloader._vision_chain(cfg) == ["vision", "mimo"]

    def test_invalid_entries_skipped(self):
        cfg = _cfg(vision={"chain": ["", 42, "mimo", None]})
        assert downloader._vision_chain(cfg) == ["mimo"]


class TestGetVisionClients:
    def test_order_follows_config_and_skips_disabled(self, monkeypatch):
        cfg = _cfg(
            models={
                "glm53_flash": {"enabled": True, "model": "glm-5.3-flash", "api_key": "k1"},
                "mimo": {"enabled": False, "model": "mimo-v2.5", "api_key": "k2"},
            },
            vision={"chain": ["glm53_flash", "mimo"]},
        )
        _patch_config(monkeypatch, cfg)

        clients = downloader._get_vision_clients()

        assert [tag for _c, _m, tag, _t, _mt in clients] == ["glm53_flash"]

    def test_unresolved_placeholder_key_skipped(self, monkeypatch):
        cfg = _cfg(
            models={"mimo": {"enabled": True, "model": "mimo-v2.5", "api_key": "${MIMO_API_KEY}"}},
            vision={"chain": ["mimo"]},
        )
        _patch_config(monkeypatch, cfg)

        assert downloader._get_vision_clients() == []

    def test_open_breaker_entry_skipped_and_checked_with_prefix(self, monkeypatch):
        cfg = _cfg(
            models={
                "glm53_flash": {"enabled": True, "model": "glm-5.3-flash", "api_key": "k1"},
                "mimo": {"enabled": True, "model": "mimo-v2.5", "api_key": "k2"},
            },
            vision={"chain": ["glm53_flash", "mimo"]},
        )
        breaker = FakeBreaker(open_names=frozenset({"vision:glm53_flash"}))
        _patch_config(monkeypatch, cfg, breaker)

        clients = downloader._get_vision_clients()

        assert [tag for _c, _m, tag, _t, _mt in clients] == ["mimo"]
        assert breaker.checked == ["vision:glm53_flash", "vision:mimo"]

    def test_unknown_chain_entry_skipped(self, monkeypatch):
        cfg = _cfg(
            models={"mimo": {"enabled": True, "model": "mimo-v2.5", "api_key": "k"}},
            vision={"chain": ["not-a-model", "mimo"]},
        )
        _patch_config(monkeypatch, cfg)

        assert [tag for _c, _m, tag, _t, _mt in downloader._get_vision_clients()] == ["mimo"]


def _write_png() -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return f.name


class TestDescribeBreakerNamespace:
    def test_success_records_namespaced_breaker_key(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="描述"))
        ]
        monkeypatch.setattr(
            downloader,
            "_get_vision_clients",
            lambda: [(mock_client, "mock-model", "glm53_flash", 30, 1536)],
        )
        breaker = FakeBreaker()
        import fin_analyse.claims.backend_health as bh

        monkeypatch.setattr(bh, "get_backend_circuit_breaker", lambda: breaker)

        tmp_path = _write_png()
        try:
            assert downloader.describe_image(tmp_path) == "描述"
        finally:
            Path(tmp_path).unlink()

        assert breaker.succeeded == ["vision:glm53_flash"]
        assert breaker.failed == []

    def test_failure_records_namespaced_breaker_key(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")
        monkeypatch.setattr(
            downloader,
            "_get_vision_clients",
            lambda: [(mock_client, "mock-model", "glm53_flash", 30, 1536)],
        )
        breaker = FakeBreaker()
        import fin_analyse.claims.backend_health as bh

        monkeypatch.setattr(bh, "get_backend_circuit_breaker", lambda: breaker)

        tmp_path = _write_png()
        try:
            assert downloader.describe_image(tmp_path) == ""
        finally:
            Path(tmp_path).unlink()

        assert breaker.failed == ["vision:glm53_flash"]
