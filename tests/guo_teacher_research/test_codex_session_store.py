"""Phase 3B：owner-only Codex session artifact store 测试。

验收：权限/symlink/hardlink/配额/原子 rename/版本 orphan/GC/FD 泄漏全过；
存储中无 auth/config/history/state DB。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.codex_session_store import (
    CodexSessionArtifactStore,
    CodexSessionStoreAlreadyExistsError,
    CodexSessionStoreInvalidError,
    CodexSessionStoreMissingError,
)

_SESSION_ID = "019fc2fe-7ea6-7e32-a20b-357f21429486"
_IDENTITY = "a" * 64
_EXECUTABLE = "b" * 64


def _rollout_bytes(seed: str) -> bytes:
    return (
        json.dumps(
            {"type": "thread.started", "thread_id": _SESSION_ID, "seed": seed},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _fake_source_home(
    tmp_path: Path,
    *,
    seeds: tuple[str, ...] = ("one", "two"),
    name: str = "source-home",
    session_id: str = _SESSION_ID,
) -> Path:
    """构造模拟 source CODEX_HOME：sessions/YYYY/MM/DD/rollout-*.jsonl。"""
    home = tmp_path / name
    day = home / "sessions" / "2026" / "08" / "02"
    day.mkdir(parents=True)
    # mkdir(parents=True, mode=0o700) 只对叶子生效——显式把整链收敛为 0700
    for directory in (
        home,
        home / "sessions",
        home / "sessions" / "2026",
        home / "sessions" / "2026" / "08",
        day,
    ):
        directory.chmod(0o700)
    for index, seed in enumerate(seeds):
        # 始终生成合法且唯一的 HH-MM-SS 时间（index≥60 时分秒进位，不溢出）
        hour, minute = divmod(index, 60)
        name = f"rollout-2026-08-02T{hour:02d}-{minute:02d}-00-{session_id}.jsonl"
        (day / name).write_bytes(_rollout_bytes(seed))
        (day / name).chmod(0o600)
    return home


def _store(tmp_path: Path) -> CodexSessionArtifactStore:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    fixed_clock = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    return CodexSessionArtifactStore(state_root=state_root, clock=lambda: fixed_clock)


def _versions_dir(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "state"
        / "runtime-sessions"
        / "codex-cli"
        / "v1"
        / "019fc2fe7ea67e32a20b357f21429486"
        / "versions"
    )


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _capture_args(store: CodexSessionArtifactStore, source: Path, *, version: int = 1, **overrides):
    kwargs = {
        "session_id": _SESSION_ID,
        "product_version": version,
        "runtime_identity_hash": _IDENTITY,
        "codex_executable_sha256": _EXECUTABLE,
        "source_home": source,
    }
    kwargs.update(overrides)
    return kwargs


# ── capture ──────────────────────────────────────────────────────────────────


def test_capture_roundtrip_materialize(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)

    manifest = store.capture(**_capture_args(store, source))
    assert manifest["schema_version"] == "fin.codex-session-artifact/v1"
    assert manifest["session_id"] == _SESSION_ID
    assert manifest["product_version"] == 1
    assert manifest["total_bytes"] > 0
    assert len(manifest["files"]) == 2
    for item in manifest["files"]:
        assert item["path"].startswith("sessions/2026/08/02/rollout-")
        assert item["size"] > 0
        assert len(item["sha256"]) == 64

    # materialize 到新 home：内容逐字节一致
    dest = tmp_path / "dest-home"
    dest.mkdir(mode=0o700)
    copied = store.materialize(
        session_id=_SESSION_ID,
        product_version=1,
        dest_home=dest,
    )
    assert copied == manifest["total_bytes"]
    materialized = sorted((dest / "sessions").rglob("rollout-*.jsonl"))
    originals = sorted(source.rglob("rollout-*.jsonl"))
    assert [p.read_bytes() for p in materialized] == [p.read_bytes() for p in originals]
    # dest 权限
    for path in dest.rglob("*.jsonl"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_capture_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    first = store.capture(**_capture_args(store, source))
    second = store.capture(**_capture_args(store, source))
    assert first == second


def test_capture_idempotency_requires_identical_executable(tmp_path: Path) -> None:
    """幂等还必须包含 codex_executable_sha256——不同 executable 不算同一版本。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    store.capture(**_capture_args(store, source))
    with pytest.raises(CodexSessionStoreAlreadyExistsError):
        store.capture(**_capture_args(store, source, codex_executable_sha256="c" * 64))


def test_capture_idempotency_detects_stored_file_tamper(tmp_path: Path) -> None:
    """幂等重读已存文件本身：stored rollout 被篡改后不再视为幂等。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    store.capture(**_capture_args(store, source))
    version_home = _versions_dir(tmp_path) / "1" / "home"
    rollout = next(version_home.rglob("rollout-*.jsonl"))
    rollout.write_bytes(b"tampered\n")
    rollout.chmod(0o600)
    with pytest.raises(CodexSessionStoreAlreadyExistsError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_overwrite_with_different_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path, seeds=("one", "two"))))
    with pytest.raises(CodexSessionStoreAlreadyExistsError):
        store.capture(
            **_capture_args(
                store, _fake_source_home(tmp_path, seeds=("changed",), name="source-home-2")
            )
        )


def test_capture_rejects_claimed_version_without_manifest(tmp_path: Path) -> None:
    """并发/崩溃留下的空认领目录：不得被 rename 覆盖，严格重读 fail closed。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    # 预先放置一个空认领目录（模拟 TOCTOU 窗口内他人创建）
    versions = _versions_dir(tmp_path)
    versions.mkdir(parents=True, mode=0o700)
    for directory in versions.parents:
        if _versions_dir(tmp_path) == directory:
            break
        if "runtime-sessions" in str(directory):
            directory.chmod(0o700)
    (versions / "1").mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))
    # 空认领目录未被替换
    assert (versions / "1").is_dir()
    assert not (versions / "1" / "manifest.json").exists()


def test_capture_excludes_auth_and_config_by_construction(tmp_path: Path) -> None:
    """capture 只枚举 sessions/**——auth/config 等按构造排除，不会进入存储。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    (source / "auth.json").write_text("{}")
    (source / "config.toml").write_text('model = "x"')
    manifest = store.capture(**_capture_args(store, source))
    assert all(str(item["path"]).startswith("sessions/") for item in manifest["files"])
    version_root = (
        tmp_path
        / "state"
        / "runtime-sessions"
        / "codex-cli"
        / "v1"
        / "019fc2fe7ea67e32a20b357f21429486"
    )
    stored_files = [
        str(p.relative_to(version_root)) for p in version_root.rglob("*") if p.is_file()
    ]
    assert not any(name in {"auth.json", "config.toml", "history.jsonl"} for name in stored_files)


def test_capture_rejects_foreign_session_rollout(tmp_path: Path) -> None:
    """rollout 文件名 UUID 必须与请求 session 一致；混入 foreign session 整体拒绝。"""
    store = _store(tmp_path)
    other = "019fc306-d0dd-7ee1-a6ce-1800f304cd1b"
    source = _fake_source_home(tmp_path, session_id=other)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))
    # 混合：一个本 session + 一个 foreign session
    mixed = _fake_source_home(tmp_path, seeds=("one",), name="source-mixed", session_id=_SESSION_ID)
    day = mixed / "sessions" / "2026" / "08" / "02"
    (day / f"rollout-2026-08-02T23-09-00-{other}.jsonl").write_bytes(b"foreign\n")
    (day / f"rollout-2026-08-02T23-09-00-{other}.jsonl").chmod(0o600)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, mixed))


def test_capture_rejects_symlink_in_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    day = source / "sessions" / "2026" / "08" / "02"
    link = day / "rollout-2026-08-02T23-09-00-019fc2fe-7ea6-7e32-a20b-357f21429486.jsonl"
    os.symlink(day / "rollout-2026-08-02T23-00-00-019fc2fe-7ea6-7e32-a20b-357f21429486.jsonl", link)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_insecure_source_directory_mode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    (source / "sessions").chmod(0o755)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_non_canonical_identity_hashes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source, runtime_identity_hash="not-a-hash"))
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source, codex_executable_sha256="short"))


def test_capture_rejects_invalid_session_id_and_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source, session_id="not-a-uuid"))
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source, product_version=0))


def test_capture_stores_version_layout_with_manifest_inside(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path), version=3))
    version_dir = _versions_dir(tmp_path) / "3"
    assert (version_dir / "manifest.json").is_file()
    assert (version_dir / "home" / "sessions").is_dir()
    # manifest 不在 versions 层（必须在版本目录内部）
    assert not (version_dir.parent / "manifest.json").exists()
    # 权限
    for path in version_dir.rglob("*"):
        if path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_capture_cleans_pending_and_claim_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实注入第二文件读取失败：pending 与空认领目录都必须回滚。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path, seeds=("one", "two"))
    real_read = store._read_source_rollout
    calls = {"n": 0}

    def flaky_read(source_home: Path, relative: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CodexSessionStoreInvalidError("injected read failure")
        return real_read(source_home, relative)

    monkeypatch.setattr(store, "_read_source_rollout", flaky_read)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))
    assert calls["n"] >= 2
    versions = _versions_dir(tmp_path)
    assert [p.name for p in versions.iterdir()] == []  # 无 pending、无空认领


# ── 恶意 manifest / 配额 ─────────────────────────────────────────────────────


def test_manifest_tamper_rejected_by_strict_decode(tmp_path: Path) -> None:
    """篡改身份/负 total_bytes/重复路径/非 canonical 顺序全部 fail closed。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    manifest_path = _versions_dir(tmp_path) / "1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for mutate in (
        lambda m: m.update(session_id="00000000-0000-0000-0000-000000000000"),
        lambda m: m.update(product_version=999),
        lambda m: m.update(total_bytes=-123),
        lambda m: m.update(total_bytes=99999999),
        lambda m: m.update(files=[m["files"][0], m["files"][0]]),
        lambda m: m.update(files=list(reversed(m["files"]))),
        lambda m: m.update(files=[{**m["files"][0], "sha256": "0" * 64}]),
        # 未知顶层字段 / 类型 coercion 一律拒绝
        lambda m: m.update(extra_field="x"),
        lambda m: m.update(files=[{**m["files"][0], "size": True}]),
        lambda m: m.update(files=[{**m["files"][0], "size": 1.5}]),
        lambda m: m.update(files=[{**m["files"][0], "sha256": 123}]),
        lambda m: m.update(files=[{**m["files"][0], "extra": "x"}]),
        lambda m: m.update(files=[{"path": m["files"][0]["path"]}]),
        lambda m: m.update(created_at="not-a-number"),
        lambda m: m.update(captured_at=None),
        lambda m: m.update(runtime_identity_hash=123),
        lambda m: m.update(codex_executable_sha256="not-hex"),
        lambda m: m.update(product_version=True),
        # 非有限时间 / 巨大 JSON 整数 / 非字符串 path / 字符串 size 一律拒绝
        lambda m: m.update(created_at=float("nan")),
        lambda m: m.update(created_at=float("inf")),
        lambda m: m.update(captured_at=float("-inf")),
        lambda m: m.update(created_at=10**309),
        lambda m: m.update(captured_at=-(10**309)),
        lambda m: m.update(files=[{**m["files"][0], "path": 123}]),
        lambda m: m.update(files=[{**m["files"][0], "size": "1"}]),
        lambda m: m.update(files=[{**m["files"][0], "sha256": ["hex"]}]),
    ):
        tampered = json.loads(json.dumps(manifest, ensure_ascii=False))
        mutate(tampered)
        manifest_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        manifest_path.chmod(0o600)
        with pytest.raises(CodexSessionStoreInvalidError):
            store._read_manifest(_SESSION_ID, 1)
        # 恢复原始 manifest 供下一轮篡改
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        manifest_path.chmod(0o600)

    # store_bytes 严格计量：损坏 manifest 一律传播为 Invalid（不静默跳过），
    # 健康版本正常计入
    assert store.store_bytes() > 0
    tampered = json.loads(json.dumps(manifest, ensure_ascii=False))
    tampered["total_bytes"] = -123
    manifest_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    manifest_path.chmod(0o600)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.store_bytes()  # 损坏版本 → 计量失败（不误报为 0）
    # 巨大 JSON 整数时间：read 与 store_bytes 都报 Invalid
    tampered = json.loads(json.dumps(manifest, ensure_ascii=False))
    tampered["created_at"] = 10**309
    manifest_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    manifest_path.chmod(0o600)
    with pytest.raises(CodexSessionStoreInvalidError):
        store._read_manifest(_SESSION_ID, 1)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.store_bytes()
    # 超长整数字面量（5000 位）在 json.loads 解析阶段溢出 → Invalid 而非 ValueError
    serialized = json.dumps(manifest, ensure_ascii=False)
    import re as _re

    huge = "-" + "9" * 5000
    serialized = _re.sub(r'"created_at": [0-9.]+', f'"created_at": {huge}', serialized, count=1)
    manifest_path.write_text(serialized, encoding="utf-8")
    manifest_path.chmod(0o600)
    with pytest.raises(CodexSessionStoreInvalidError):
        store._read_manifest(_SESSION_ID, 1)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.store_bytes()


def test_capture_rejects_version_over_32mib(tmp_path: Path) -> None:
    """单版本 32 MiB 配额：超限直接拒绝，不写盘。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path, seeds=("one",))
    # 把一个 rollout 撑到超限
    day = source / "sessions" / "2026" / "08" / "02"
    rollout = next(day.glob("rollout-*.jsonl"))
    rollout.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
    rollout.chmod(0o600)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_empty_source(tmp_path: Path) -> None:
    """空 source 不得发布自己无法重新读取的空版本。"""
    store = _store(tmp_path)
    source = tmp_path / "empty-home"
    source.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_oversized_manifest(tmp_path: Path) -> None:
    """manifest 超过 64 KiB 读取上限：发布前拒绝，不能发布后读不回。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path, seeds=tuple(f"s{i}" for i in range(500)))
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


# ── FD 泄漏回归 ──────────────────────────────────────────────────────────────


def test_no_fd_leak_on_capture_and_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    before = _fd_count()
    store.capture(**_capture_args(store, source))
    for _ in range(20):
        store._read_manifest(_SESSION_ID, 1)
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)
    assert _fd_count() == before


def test_no_fd_leak_on_capture_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path, seeds=("one", "two"))
    real_read = store._read_source_rollout
    calls = {"n": 0}

    def flaky_read(source_home: Path, relative: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CodexSessionStoreInvalidError("injected")
        return real_read(source_home, relative)

    monkeypatch.setattr(store, "_read_source_rollout", flaky_read)
    before = _fd_count()
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))
    assert _fd_count() == before


def test_no_fd_leak_on_missing_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = _fd_count()
    with pytest.raises(CodexSessionStoreMissingError):
        store._read_manifest(_SESSION_ID, 1)
    with pytest.raises(CodexSessionStoreMissingError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=tmp_path)
    assert _fd_count() == before


def test_no_fd_leak_when_leaf_open_fails(tmp_path: Path) -> None:
    """父目录已打开、叶文件 open 失败的真实路径：rollout 缺失与 manifest 缺失都不泄漏。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    versions = _versions_dir(tmp_path)
    # 1) stored rollout 实际删除 → materialize 走 stored leaf-open 失败路径
    manifest_path = versions / "1" / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    rollout_path = versions / "1" / "home" / original["files"][0]["path"]
    rollout_path.unlink()
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    before = _fd_count()
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)
    assert _fd_count() == before
    # 2) 版本目录存在但 manifest 被删除 → 每次读取都走 manifest leaf-open 失败路径
    manifest_path.unlink()
    before = _fd_count()
    for _ in range(20):
        with pytest.raises(CodexSessionStoreInvalidError):
            store._read_manifest(_SESSION_ID, 1)
    assert _fd_count() == before


# ── materialize ──────────────────────────────────────────────────────────────


def test_no_fd_leak_on_post_open_stat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """post-open stat 失败（name race）→ Invalid，且无 FD 泄漏。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    store.capture(**_capture_args(store, source))
    real_stat = os.stat

    def racy_stat(name, dir_fd=None, follow_symlinks=True):
        if isinstance(name, str) and name == "manifest.json" and dir_fd is not None:
            raise FileNotFoundError(f"race on {name}")
        return real_stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("fin_analyse.guo_teacher_research.codex_session_store.os.stat", racy_stat)
    before = _fd_count()
    with pytest.raises(CodexSessionStoreInvalidError):
        store._read_manifest(_SESSION_ID, 1)
    assert _fd_count() == before


def test_materialize_missing_version_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreMissingError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


def test_materialize_rejects_insecure_dest(tmp_path: Path) -> None:
    """dest_home 必须 owner-only：0755 拒绝。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    dest = tmp_path / "wide-dest"
    dest.mkdir(mode=0o755)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


def test_materialize_rejects_loose_stored_rollout_mode(tmp_path: Path) -> None:
    """stored rollout 必须 0600：0644 拒绝。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    rollout = next((_versions_dir(tmp_path) / "1" / "home").rglob("rollout-*.jsonl"))
    rollout.chmod(0o644)
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


def test_capture_rejects_hardlinked_source_rollout(tmp_path: Path) -> None:
    """source rollout 被外部硬链接（nlink>1，文件名合法）：拒绝。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path, seeds=("one",))
    day = source / "sessions" / "2026" / "08" / "02"
    rollout = next(day.glob("rollout-*.jsonl"))
    # 合法 rollout 文件名 + nlink>1：只有 nlink 校验能拦下
    os.link(rollout, day / f"rollout-2026-08-02T09-30-00-{_SESSION_ID}.jsonl")
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))


def test_materialize_rejects_hardlinked_stored_rollout(tmp_path: Path) -> None:
    """stored rollout 被外部硬链接（nlink>1，文件名合法）：materialize 拒绝。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    version_home = _versions_dir(tmp_path) / "1" / "home"
    rollout = next(version_home.rglob("rollout-*.jsonl"))
    os.link(rollout, rollout.parent / f"rollout-2026-08-02T09-30-00-{_SESSION_ID}.jsonl")
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


def test_materialize_rejects_insecure_stored_inner_dir(tmp_path: Path) -> None:
    """stored tree 中间目录 0755：materialize 拒绝。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    day = _versions_dir(tmp_path) / "1" / "home" / "sessions" / "2026" / "08" / "02"
    day.chmod(0o755)
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


def test_capture_rejects_extra_stored_rollout_not_idempotent(tmp_path: Path) -> None:
    """exact-set 幂等：stored tree 出现 manifest 未列的额外 rollout → 非幂等。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    store.capture(**_capture_args(store, source))
    version_home = _versions_dir(tmp_path) / "1" / "home"
    day = version_home / "sessions" / "2026" / "08" / "02"
    (day / "rollout-extra.jsonl").write_bytes(b"extra\n")
    (day / "rollout-extra.jsonl").chmod(0o600)
    with pytest.raises(CodexSessionStoreAlreadyExistsError):
        store.capture(**_capture_args(store, source))


def test_capture_rejects_extra_stored_topology_not_idempotent(tmp_path: Path) -> None:
    """exact-tree 幂等：版本根/home 额外文件、空目录都判定非幂等。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    store.capture(**_capture_args(store, source))
    version_dir = _versions_dir(tmp_path) / "1"
    for extra in (
        # 版本根额外普通文件
        lambda vd=version_dir: (vd / "extra-file").write_bytes(b"x"),
        # home/ 额外文件
        lambda vd=version_dir: (vd / "home" / "extra").write_bytes(b"x"),
        # sessions/ 下额外空年份目录
        lambda vd=version_dir: (vd / "home" / "sessions" / "2027").mkdir(mode=0o700),
        # sessions/ 下额外 symlink
        lambda vd=version_dir: os.symlink(
            vd / "home" / "sessions",
            vd / "home" / "sessions" / "link",
        ),
    ):
        extra()
        with pytest.raises(CodexSessionStoreAlreadyExistsError):
            store.capture(**_capture_args(store, source))
        # 还原拓扑（删除新增项）
        version_dir = _versions_dir(tmp_path) / "1"
        for candidate in version_dir.rglob("extra-file"):
            candidate.unlink()
        for candidate in version_dir.rglob("extra"):
            candidate.unlink()
        for candidate in version_dir.rglob("link"):
            candidate.unlink()
        stray = version_dir / "home" / "sessions" / "2027"
        if stray.is_dir() and not any(stray.iterdir()):
            stray.rmdir()


def test_capture_claim_replacement_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU：pending 写入后、发布前出现并发 claim → renameat2 不覆盖。"""
    store = _store(tmp_path)
    source = _fake_source_home(tmp_path)
    versions = _versions_dir(tmp_path)
    real_write = store._write_pending

    def create_competing_claim_after_pending(*args, **kwargs) -> None:
        real_write(*args, **kwargs)
        # 模拟并发方在 pending 写入期间认领版本槽
        (versions / "1").mkdir(mode=0o700)

    monkeypatch.setattr(store, "_write_pending", create_competing_claim_after_pending)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.capture(**_capture_args(store, source))
    # 并发 claim 槽未被覆盖，也没有发布内容
    assert (versions / "1").is_dir()
    assert not (versions / "1" / "manifest.json").exists()
    # 清理：没有残留 pending
    assert [p.name for p in versions.iterdir() if p.name.startswith("pending-")] == []


def test_sweep_orphans_ignores_unknown_names(tmp_path: Path) -> None:
    """sweep 只删 canonical pending/空版本目录；未知路径绝不删除。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    versions = _versions_dir(tmp_path)
    unknown = versions / "do-not-delete"
    unknown.mkdir(mode=0o700)
    assert store.sweep_orphans() == 0
    assert unknown.exists()


def test_sweep_orphans_ignores_unknown_session_tree(tmp_path: Path) -> None:
    """sweep 绝不进入未知 session 父树（非 32-hex 名称）删除内容。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    sessions_root = tmp_path / "state" / "runtime-sessions" / "codex-cli" / "v1"
    unknown_session = sessions_root / "unknown-session"
    versions = unknown_session / "versions"
    versions.mkdir(parents=True, mode=0o700)
    for directory in (
        unknown_session,
        versions,
    ):
        directory.chmod(0o700)
    orphan = versions / "pending-deadbeefdeadbeefdeadbeefdeadbeef.tmp"
    orphan.mkdir(mode=0o700)
    assert store.sweep_orphans() == 0
    assert orphan.exists()


def test_materialize_rejects_corrupt_manifest_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    manifest_path = _versions_dir(tmp_path) / "1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    manifest_path.chmod(0o600)
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o700)
    with pytest.raises(CodexSessionStoreInvalidError):
        store.materialize(session_id=_SESSION_ID, product_version=1, dest_home=dest)


# ── delete / sweep / 配额 ───────────────────────────────────────────────────


def test_delete_version_removes_tree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    store.delete_version(session_id=_SESSION_ID, product_version=1)
    version_dir = _versions_dir(tmp_path) / "1"
    assert not version_dir.exists()
    with pytest.raises(CodexSessionStoreMissingError):
        store.delete_version(session_id=_SESSION_ID, product_version=1)


def test_sweep_orphans_removes_deep_pending_dirs(tmp_path: Path) -> None:
    """真实深度 orphan：pending-*.tmp/home/sessions/YYYY/MM/DD/rollout-*.jsonl。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    versions = _versions_dir(tmp_path)
    orphan = versions / "pending-deadbeefdeadbeefdeadbeefdeadbeef.tmp"
    day = orphan / "home" / "sessions" / "2026" / "08" / "02"
    day.mkdir(parents=True, mode=0o700)
    (day / "rollout-x.jsonl").write_bytes(b"junk")
    # mkdir(parents=True) 只收敛叶子——整链显式 0700
    for directory in (
        orphan,
        orphan / "home",
        orphan / "home" / "sessions",
        orphan / "home" / "sessions" / "2026",
        orphan / "home" / "sessions" / "2026" / "08",
        day,
    ):
        directory.chmod(0o700)
    (day / "rollout-x.jsonl").chmod(0o600)
    assert store.sweep_orphans() == 1
    assert not orphan.exists()
    assert store.sweep_orphans() == 0


def test_sweep_orphans_removes_empty_claim_dirs(tmp_path: Path) -> None:
    """空认领目录（capture 认领后崩溃）由 sweep 清理，不阻塞后续版本。"""
    store = _store(tmp_path)
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    versions = _versions_dir(tmp_path)
    (versions / "2").mkdir(mode=0o700)
    assert store.sweep_orphans() == 1
    assert not (versions / "2").exists()
    # 真实版本不受影响
    assert (versions / "1" / "manifest.json").is_file()


def test_store_bytes_accounts_versions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.store_bytes() == 0
    store.capture(**_capture_args(store, _fake_source_home(tmp_path)))
    assert store.store_bytes() > 0
