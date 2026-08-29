"""B4: operator alert CLI tests (ledger-based, fake composition)."""

from __future__ import annotations

import json


class _Row:
    def __init__(self, run_id: str, stage_statuses: str) -> None:
        self.run_id = run_id
        self.stage_statuses = stage_statuses

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


def _stages(*entries: tuple[str, str]) -> str:
    return json.dumps(
        [{"stage": stage, "status": status, "degraded": False, "detail": ""}
         for stage, status in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _snapshot(row=None, freshness=None):
    return lambda day: (row, freshness)


def test_alert_no_rows_is_no_alert(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(None, None),
        sender=lambda m: "m",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert"] == "no_alert"


def test_alert_stage_failure_sends_and_records(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    sent: list[str] = []

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-1", _stages(("collect", "COLLECT_FAILED"))), None
        ),
        sender=lambda m: sent.append(m) or "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert"] == "sent"
    assert payload["alert_kind"] == "run_stage_failure"
    assert "COLLECT_FAILED" in payload["reason"]
    assert len(sent) == 1
    assert "run-1" in sent[0]


def test_alert_window_missed_triggers(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-2", _stages(("prepare", "WINDOW_MISSED"))), "FRESH"
        ),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert_kind"] == "run_stage_failure"
    assert "WINDOW_MISSED" in payload["reason"]


def test_alert_stale_freshness_triggers(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-3", _stages(("collect", "COLLECT_READY"), ("prepare", "PREPARED"), ("deliver", "DELIVERED"))),
            "STALE",
        ),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert_kind"] == "g_freshness_stale"


def test_alert_untrusted_freshness_with_rows_triggers(capsys) -> None:
    """有行但 freshness None（无可信新采集）→ 告警（区别于空行 no_alert）。"""
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-4", _stages(("collect", "COLLECT_PARTIAL"), ("prepare", "PREPARED"), ("deliver", "DELIVERED"))),
            None,
        ),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert_kind"] == "g_freshness_untrusted"


def test_alert_fresh_ok_run_is_no_alert(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-5", _stages(("collect", "COLLECT_READY"), ("prepare", "PREPARED"), ("deliver", "DELIVERED"))),
            "FRESH",
        ),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert"] == "no_alert"


def test_alert_unknown_outcome_records_and_no_resend(capsys) -> None:
    """send 返回 None → OUTCOME_UNKNOWN 落账；不自动重发（单次调用一条）。"""
    import scripts.alert_daily_workspace as cli

    sent: list[str] = []

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-6", _stages(("collect", "COLLECT_FAILED"))), None
        ),
        sender=lambda m: sent.append(m) or None,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["send_outcome"] == "OUTCOME_UNKNOWN"
    assert len(sent) == 1


def test_alert_sender_raises_records_unknown(capsys) -> None:
    import scripts.alert_daily_workspace as cli


    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-7", _stages(("collect", "COLLECT_FAILED"))), None
        ),
        sender=lambda m: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["send_outcome"] == "OUTCOME_UNKNOWN"


def test_alert_ledger_read_failure_is_typed(capsys) -> None:
    import scripts.alert_daily_workspace as cli

    def broken_reader(day):
        raise RuntimeError("daily_workspace_state_root_insecure")

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=broken_reader,
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["alert"] == "failed"


def test_alert_unknown_freshness_triggers(capsys) -> None:
    """B2 合法 freshness=UNKNOWN（已有真实采集但未知）→ 告警，不静默漏报。"""
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-8", _stages(("collect", "COLLECT_READY"), ("prepare", "PREPARED"), ("deliver", "DELIVERED"))),
            "UNKNOWN",
        ),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alert_kind"] == "g_freshness_unknown"


def test_alert_reason_includes_stage_detail(capsys) -> None:
    """失败 stage 的逐字 detail 进入告警原因（不丢窄原因）。"""
    import scripts.alert_daily_workspace as cli

    stages = json.dumps(
        [{"stage": "collect", "status": "COLLECT_FAILED", "degraded": False,
          "detail": "zsxq_runtime_db_missing"}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(_Row("run-9", stages), None),
        sender=lambda m: "message-1",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "zsxq_runtime_db_missing" in payload["reason"]


def test_alert_unknown_outcome_is_attempted_not_sent(capsys) -> None:
    """OUTCOME_UNKNOWN 输出为 attempted（不冒充 sent）。"""
    import scripts.alert_daily_workspace as cli

    code = cli.main(
        ["--trade-date", "2026-08-06", "--delivery-target", "feishu:oc_test"],
        snapshot_reader=_snapshot(
            _Row("run-10", _stages(("collect", "COLLECT_FAILED"))), None
        ),
        sender=lambda m: None,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["alert"] == "attempted"
    assert payload["send_outcome"] == "OUTCOME_UNKNOWN"
