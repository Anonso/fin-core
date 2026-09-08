"""退役材料墓碑清扫（设计稿 docs/design/zsxq-retirement-tombstone.md，设计门 20260908）。

registry = config/zsxq_retired.json；清扫器按墓碑行核身三条件
（manifest 可解析 + items 含该 article_id + 现盘 sha256 与 manifest 一致）
后才删 KB 内复活的退役文章，index 条目同步清除。
零 stdout（consumer stdout=单 JSON 契约）；registry 缺失/损坏=不清扫不报错
（fail-open，与 consumer 挂点纪律一致）；永不调用方进程、不阻断 ingest。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_SCHEMA = "fin.zsxq-retired/v1"
SWEEP_SCHEMA = "fin.retired-sweep/v1"
_DATA_HOME = Path.home() / ".local" / "share" / "fin-analyse"


def load_registry(path: Path) -> list[dict[str, str]]:
    """读墓碑登记表；缺失/损坏/非法 → 空表（fail-open）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != REGISTRY_SCHEMA:
        return []
    rows = payload.get("retired")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        aid, ref = row.get("id"), row.get("backup_ref")
        if not isinstance(aid, str) or not aid.startswith("zsxq-"):
            continue
        if not isinstance(ref, str) or not ref or ref.startswith("/"):
            continue
        out.append({"id": aid, "backup_ref": ref,
                    "retired_at": str(row.get("retired_at", "")),
                    "reason": str(row.get("reason", ""))})
    return out


def _manifest_entry(backup_dir: Path, article_id: str) -> dict | None:
    """备份 manifest 中该 article_id 的 article_file 条目；无则 None。"""
    try:
        payload = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if (isinstance(item, dict)
                and item.get("type") == "article_file"
                and item.get("article_id") == article_id):
            return item
    return None


def _find_article_md(kb_root: Path, article_id: str) -> Path | None:
    topic = article_id.removeprefix("zsxq-")
    matches = list((kb_root / "articles").glob(f"*zsxq-{topic}.md"))
    return matches[0] if matches else None


def _rewrite_index(kb_root: Path, remove_ids: set[str]) -> bool:
    """index.json 手术移除条目；原子替换、0600、无变更不落盘。返回是否落盘。"""
    index_path = kb_root / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return False
    kept = [a for a in articles
            if not (isinstance(a, dict) and a.get("id") in remove_ids)]
    if len(kept) == len(articles):
        return False
    payload["articles"] = kept
    payload["total"] = len(kept)
    payload["updated"] = (
        datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    )
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(index_path.parent), suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, index_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


def sweep_retired_articles(
    kb_root: Path, registry_path: Path, *, data_home: Path = _DATA_HOME
) -> dict:
    """按 registry 清扫复活的退役材料；返回 audit payload（不落盘、零 stdout）。"""
    actions: list[dict[str, str]] = []
    warns: list[dict[str, str]] = []
    index_remove: set[str] = set()
    registry = load_registry(registry_path)
    for tomb in registry:
        aid = tomb["id"]
        backup_dir = (
            Path(tomb["backup_ref"])
            if Path(tomb["backup_ref"]).is_absolute()
            else data_home / tomb["backup_ref"]
        )
        entry = _manifest_entry(backup_dir, aid)
        md = _find_article_md(kb_root, aid)
        if md is not None:
            if entry is None:
                warns.append({"id": aid,
                              "warn": "manifest_entry_missing——不删（家规4：无核身备份不删 owner 数据）"})
            else:
                digest = hashlib.sha256(md.read_bytes()).hexdigest()
                if digest != entry.get("sha256"):
                    warns.append({"id": aid,
                                  "warn": "sha_mismatch——不删（防 typo id 误删活文章）"})
                else:
                    md.unlink()
                    actions.append({"id": aid, "action": "removed_md",
                                    "sha256": digest})
            index_remove.add(aid)
        elif entry is not None:
            index_remove.add(aid)
    if index_remove:
        if _rewrite_index(kb_root, index_remove):
            actions.append({"action": "index_entries_removed",
                            "ids": ",".join(sorted(index_remove))})
    return {
        "schema_version": SWEEP_SCHEMA,
        "disposition": "SWEPT" if actions else ("WARNED" if warns else "CLEAN"),
        "actions": actions,
        "warns": warns,
        "registry_size": len(registry),
    }
