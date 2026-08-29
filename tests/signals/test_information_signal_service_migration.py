"""Migration guard — production code must not directly instantiate old signal engines."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {
    "fin_analyse/signals/engine.py",
    "fin_analyse/dynamics/signal_engine.py",
    "fin_analyse/signals/information.py",
    "fin_analyse/signals/__init__.py",
}


def test_production_callers_use_information_signal_service() -> None:
    offenders: list[str] = []
    patterns = (
        "from fin_analyse.signals.engine import SignalEngine",
        "from fin_analyse.dynamics.signal_engine import DynamicSignalEngine",
        "SignalEngine(",
        "DynamicSignalEngine(",
    )
    for path in sorted((ROOT / "fin_analyse").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        if any(pattern in source for pattern in patterns):
            offenders.append(rel)
    assert offenders == [], (
        "production callers still directly instantiate old signal engines: "
        + ", ".join(offenders)
    )
