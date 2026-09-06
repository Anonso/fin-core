"""fin-adjudication CLI 单测（list/show/done/add/push --dry-run）。"""

from pathlib import Path

import pytest

from fin_analyse.adjudication import cli


@pytest.fixture()
def state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "adjudication-inbox-v1"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return root


def _run(argv: list[str]) -> int:
    return cli.main(argv)


def test_list_empty(state_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(["list"]) == 0
    assert "无待裁决项" in capsys.readouterr().out


def test_list_show_done_roundtrip(
    state_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _run(
            [
                "add",
                "--title",
                "BUG-058 分母合同裁决",
                "--hint",
                "拍板其一后施工",
                "--ref",
                "docs/pm/BUGS.md#L957",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("已登记：manual:")
    item_id = out.removeprefix("已登记：").strip()

    assert _run(["list"]) == 0
    listed = capsys.readouterr().out
    assert "BUG-058 分母合同裁决" in listed
    assert "拍板其一后施工" in listed

    assert _run(["show", item_id]) == 0
    shown = capsys.readouterr().out
    assert "status: open" in shown
    assert "docs/pm/BUGS.md#L957" in shown

    assert _run(["done", item_id, "--note", "排除该类，改分母合同"]) == 0
    assert _run(["show", item_id]) == 0
    shown = capsys.readouterr().out
    assert "status: resolved" in shown
    assert "resolve_source: owner" in shown
    assert "note: 排除该类，改分母合同" in shown


def test_done_missing_item_fails(state_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(["done", "manual:ghost"]) == 1
    assert "adjudication_item_not_open" in capsys.readouterr().err


def test_push_dry_run_renders_without_target(
    state_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(["add", "--title", "待裁决样例", "--hint", "命令一句话"])
    monkeypatch.delenv("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", raising=False)
    assert _run(["push", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "1 项待裁决" in captured.out
    assert "disposition: dry_run" in captured.err


def test_push_without_target_fails_typed(
    state_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(["add", "--title", "待裁决样例"])
    monkeypatch.delenv("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", raising=False)
    assert _run(["push"]) == 1
    assert "FIN_DAILY_WORKSPACE_DELIVERY_TARGET" in capsys.readouterr().err


def test_push_real_send_uses_hermes_sender(
    state_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真发路径走 HermesCliMessageSender；用注入 fake 替身验证接线与落账。"""

    from fin_analyse.operations import daily_workspace_delivery as delivery_module

    _run(["add", "--title", "真发样例"])
    monkeypatch.setenv("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", "feishu:ctl")

    sent: list[str] = []

    class _Sender:
        def __init__(self, *, target: str, timeout_seconds: float = 30.0) -> None:
            self.target = target

        def send(self, message: str) -> str | None:
            sent.append(message)
            return "mid-42"

    monkeypatch.setattr(delivery_module, "HermesCliMessageSender", _Sender)
    assert _run(["push"]) == 0
    captured = capsys.readouterr()
    assert "disposition: sent" in captured.out
    assert len(sent) == 1
    # 落账可查：同日重推被去重挡住
    assert _run(["push"]) == 0
    assert "disposition: already_sent" in capsys.readouterr().out
    assert len(sent) == 1
