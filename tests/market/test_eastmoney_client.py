from unittest.mock import Mock

import pytest

from fin_analyse.market import eastmoney_client as mod


def test_strip_jsonp_returns_inner_json():
    assert mod.strip_jsonp('callback({"data": 1})') == '{"data": 1}'


def test_strip_jsonp_leaves_plain_json_unchanged():
    assert mod.strip_jsonp('{"data": 1}') == '{"data": 1}'


def test_em_get_uses_session_headers_and_throttles(monkeypatch):
    sleeps: list[float] = []
    times = iter([10.0, 10.2])
    response = Mock()
    session = Mock()
    session.get.return_value = response
    session.headers = {}

    monkeypatch.setattr(mod, "_EM_SESSION", session)
    monkeypatch.setattr(mod, "_em_last_call", [9.8])
    monkeypatch.setattr(mod.time, "time", lambda: next(times))
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(mod.random, "uniform", lambda start, end: 0.1)

    result = mod.em_get("https://push2.eastmoney.com/api/test", params={"a": "b"})

    assert result is response
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.9, abs=1e-2)
    session.get.assert_called_once()


def test_eastmoney_datacenter_returns_data(monkeypatch):
    response = Mock()
    response.json.return_value = {"result": {"data": [{"SECURITY_CODE": "600519"}]}}

    calls = []

    def fake_em_get(url, params=None, timeout=15, **kwargs):
        calls.append((url, params, timeout))
        return response

    monkeypatch.setattr(mod, "em_get", fake_em_get)

    rows = mod.eastmoney_datacenter(
        report_name="RPT_DAILYBILLBOARD_DETAILS",
        filter_str='(SECURITY_CODE="600519")',
        page_size=20,
        sort_columns="TRADE_DATE",
    )

    assert rows == [{"SECURITY_CODE": "600519"}]
    assert calls[0][0] == mod.DATACENTER_URL
    assert calls[0][1]["reportName"] == "RPT_DAILYBILLBOARD_DETAILS"
    assert calls[0][1]["filter"] == '(SECURITY_CODE="600519")'
    assert calls[0][1]["pageSize"] == "20"
    assert calls[0][1]["sortColumns"] == "TRADE_DATE"


def test_eastmoney_datacenter_empty_result_returns_empty(monkeypatch):
    response = Mock()
    response.json.return_value = {"result": {"data": []}}
    monkeypatch.setattr(mod, "em_get", lambda *args, **kwargs: response)

    assert mod.eastmoney_datacenter("UNKNOWN") == []
