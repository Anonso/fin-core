from unittest.mock import Mock

import pytest

from fin_analyse.market import tdx_client as mod


def test_tdx_client_uses_first_reachable_server(monkeypatch):
    calls = []

    def fake_probe(ip, port, timeout=2.0):
        calls.append((ip, port))
        return ip == "2.2.2.2"

    factory = Mock(return_value="client")
    monkeypatch.setattr(mod, "_TDX_SERVERS", [("1.1.1.1", 7709), ("2.2.2.2", 7709)])
    monkeypatch.setattr(mod, "_probe", fake_probe)
    monkeypatch.setattr(mod.Quotes, "factory", factory)

    assert mod.tdx_client() == "client"
    assert calls == [("1.1.1.1", 7709), ("2.2.2.2", 7709)]
    factory.assert_called_once_with(market="std", server=("2.2.2.2", 7709))


def test_tdx_client_falls_back_to_bestip(monkeypatch):
    factory = Mock(return_value="bestip-client")
    monkeypatch.setattr(mod, "_TDX_SERVERS", [("1.1.1.1", 7709)])
    monkeypatch.setattr(mod, "_probe", lambda ip, port, timeout=2.0: False)
    monkeypatch.setattr(mod.Quotes, "factory", factory)

    assert mod.tdx_client() == "bestip-client"
    factory.assert_called_once_with(market="std", bestip=True)


def test_tdx_client_raises_clear_error_when_all_fallbacks_fail(monkeypatch):
    factory = Mock(side_effect=ValueError("not enough values to unpack"))
    monkeypatch.setattr(mod, "_TDX_SERVERS", [("1.1.1.1", 7709)])
    monkeypatch.setattr(mod, "_probe", lambda ip, port, timeout=2.0: False)
    monkeypatch.setattr(mod.Quotes, "factory", factory)

    with pytest.raises(RuntimeError, match="mootdx"):
        mod.tdx_client()
