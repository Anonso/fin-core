"""Owner 政策守卫（D-052，2026-09-07）：Windows 浏览器只允许 ZSXQ 域使用。

``opencli``/``powershell`` 是 Windows Chrome 的唯一驱动面。扫描域 =
``fin_analyse/**.py``，唯一豁免子树 = ``fin_analyse/scraper/``（ZSXQ 抓取域）。
``scripts/*.cjs`` 等 ZSXQ Windows 资产在扫描域外——非 Python 树，且由
``tests/scripts/`` 的 zsxq 用例覆盖；守卫只防 Python 生产代码再搭
Windows 浏览器的车。
"""

from __future__ import annotations

from pathlib import Path

_FIN_ANALYSE_ROOT = Path(__file__).parents[2] / "fin_analyse"
_ALLOWED_SUBTREE = "scraper"
_FORBIDDEN_TOKENS = ("opencli", "powershell")


def _policy_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(_FIN_ANALYSE_ROOT.rglob("*.py")):
        if path.relative_to(_FIN_ANALYSE_ROOT).parts[0] == _ALLOWED_SUBTREE:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for token in _FORBIDDEN_TOKENS:
            if token in text:
                violations.append(f"{path}: contains '{token}'")
    return violations


def test_windows_browser_is_confined_to_the_zsxq_domain() -> None:
    violations = _policy_violations()

    assert violations == [], (
        "Windows 浏览器驱动面（opencli/powershell）只允许存在于 "
        f"fin_analyse/{_ALLOWED_SUBTREE}/（ZSXQ 域，D-052）；违规：\n"
        + "\n".join(violations)
    )
