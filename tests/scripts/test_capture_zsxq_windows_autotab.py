"""findOrCreateGroupTab 单元测试（capture 自动 tab new 自愈分支）。

通过 node -e 注入 fake listTabs/runOpencli/sleepMs 调用导出 helper，断言
返回值与 open 调用次数。覆盖 review round2/3 冻结的触发边界：
仅严格合法空数组才 open；非空无 target/多 target 不 open。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_CAPTURE = Path(__file__).resolve().parents[2] / "scripts" / "capture_zsxq_windows.cjs"
_GROUP_URL = "https://wx.zsxq.com/group/15522441811252"


def _run(sequences_js: str) -> dict:
    script = f"""
const {{ findOrCreateGroupTab, GROUP_URL }} = require({str(_CAPTURE)!r});
const calls = [];
{sequences_js}
(async () => {{
  const result = await findOrCreateGroupTab(listTabs, (argv) => {{ calls.push(argv); return {{ok: true}}; }}, (ms) => new Promise((r) => setTimeout(r, 0)));
  console.log(JSON.stringify({{result, openCalls: calls.length}}));
}})();
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_existing_single_target_does_not_open() -> None:
    out = _run(
        """
const listTabs = () => [{url: GROUP_URL, page: "p1", title: "t"}];
"""
    )
    assert out["result"]["ok"] is True
    assert out["result"]["target"]["page"] == "p1"
    assert out["openCalls"] == 0


def test_empty_inventory_opens_and_recovers() -> None:
    out = _run(
        """
let n = 0;
const listTabs = () => (n++ === 0) ? [] : [{url: GROUP_URL + "?_fin_ts=123", page: "p2"}];
"""
    )
    assert out["result"]["ok"] is True
    assert out["result"]["target"]["page"] == "p2"
    assert out["openCalls"] == 1


def test_empty_inventory_three_rounds_fails() -> None:
    out = _run(
        """
const listTabs = () => [];
"""
    )
    assert out["result"]["ok"] is False
    assert out["result"]["reason"] == "missing"
    assert out["openCalls"] == 3


def test_nonempty_without_target_never_opens() -> None:
    out = _run(
        """
const listTabs = () => [{url: "https://example.com/other", page: "p9"}];
"""
    )
    assert out["result"]["ok"] is False
    assert out["result"]["reason"] == "missing_nonempty"
    assert out["openCalls"] == 0


def test_ambiguous_targets_never_opens() -> None:
    out = _run(
        """
const listTabs = () => [{url: GROUP_URL, page: "p1"}, {url: GROUP_URL, page: "p2"}];
"""
    )
    assert out["result"]["ok"] is False
    assert out["result"]["reason"] == "ambiguous"
    assert out["openCalls"] == 0


def test_registration_lag_recovers_on_second_round() -> None:
    out = _run(
        """
let n = 0;
const listTabs = () => {
  n += 1;
  if (n <= 2) return [];
  return [{url: GROUP_URL, page: "p4"}];
};
"""
    )
    assert out["result"]["ok"] is True
    assert out["result"]["target"]["page"] == "p4"
    assert out["openCalls"] == 2
