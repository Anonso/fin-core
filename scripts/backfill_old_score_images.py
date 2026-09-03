"""老文章定向识图：把 5/13–6/23 图片里的评分表转录进 md「## 图片描述」。

背景（D-037 扩展回填，2026-09-03）：index 内 2026-06-24 前的普通栏能量
≥6 文章有 154 篇只有图片、md 无「## 图片描述」节，结构化评分注册表无法
解析。本脚本用 vision 链逐张识图，要求模型按代码前置 inline 格式输出
（与 parser v2 对齐），成功后把结果以现行「## 图片描述」节格式追加到
文章 md（0600/原子替换），随后由 backfill_instrument_scores 正常回填。

幂等/续跑：已含「## 图片描述」的文章默认跳过（--force 可重跑）；
vision 失败不写盘，下次重跑会再试。默认 dry-run 只列候选。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from fin_analyse.runtime.knowledge_root import default_knowledge_base_root
from fin_analyse.scraper.downloader import describe_image_with_provenance

_SCORE_PROMPT = (
    "你是投研评分表转录器。先判断图片是否为“公司评分表/研报评分表”"
    "（表头常含公司名称、代码、核心业务、所属板块、利好度、共识度、"
    "预计多久启动、期待周期等字段）。\n"
    "如果是：请逐行完整转录每家公司，不要省略任何一家，统一用如下格式：\n"
    "代码 名称：核心业务为…，所属板块为…，利好度X，共识度Y\n"
    "（A股/基金/ETF 代码一律写 6 位数字，不要加 .SH/.SZ/.BJ 后缀，例如"
    "688008 而不是 688008.SH；港股维持 4 位 + .HK，如 1651.HK；若一行出现"
    "多个代码只保留主公司那个）。\n"
    "（X 为利好度原值；Y 若大于 10 请除以 10，如 88 写 8.8；若只有单侧"
    "分数也照实转录）。有“预计多久启动/持有周期”时在行尾补“，预计…启动、"
    "持有周期约…”。\n"
    "如果不是评分表（K线图、走势图、普通文字截图等）：简要描述内容并"
    "在末尾单独写一行“非评分表”。\n"
    "全部用中文回答，不要解释过程。"
)

_LEAD_NAME_RE = re.compile(
    r"([\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9·&（）()\-]*?)\s*$"
)
_CODE_TOKEN_RE = re.compile(r"\b[0-9]{4,6}(?:\.[A-Z]{2})?\b")


def load_a_share_map(kb_root: Path) -> dict[str, dict[str, Any]]:
    path = kb_root / "runtime" / "a_share_name_map.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def normalize_vision_text(text: str, name_map: dict[str, dict[str, Any]]) -> tuple[str, int]:
    """把已知 A 股行的代码归一为名册 ticker（港股/ETF/未知行不动）。"""
    corrected = 0
    output: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            output.append(raw)
            continue
        colons = [index for index, ch in enumerate(raw) if ch in "：:"]
        if not colons:
            output.append(raw)
            continue
        lead = raw[: colons[0]]
        rest = raw[colons[0] + 1 :]
        name_match = _LEAD_NAME_RE.search(lead)
        if name_match is None:
            output.append(raw)
            continue
        name = name_match.group(1).strip()
        entry = name_map.get(name)
        if entry is None:
            output.append(raw)
            continue
        expected = str(entry.get("ticker") or "")
        if not expected.isdigit():
            output.append(raw)
            continue
        output.append(f"{expected} {name}:{rest}")
        corrected += 1
    return "\n".join(output), corrected


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _frontmatter_images(md_text: str) -> list[str]:
    match = re.search(r"(?m)^images:\s*\[(.*?)\]", md_text)
    if match is None:
        return []
    return [
        item.strip().strip("'\"")
        for item in match.group(1).split(",")
        if item.strip()
    ]


def _article_md_path(kb_root: Path, row: dict[str, Any]) -> Path | None:
    raw = row.get("path") or row.get("file")
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(kb_root) / candidate
    if candidate.is_file() and not candidate.is_symlink():
        return candidate
    return None


def _write_image_desc_section(path: Path, section: str) -> None:
    original = path.read_text(encoding="utf-8")
    marker = "## 图片描述"
    if marker in original:
        start = original.index(marker)
        end = len(original)
        tail_match = re.search(r"\n##\s", original[start + len(marker) :])
        if tail_match:
            end = start + len(marker) + tail_match.start()
        body = original[:start] + section.rstrip("\n") + original[end:]
    else:
        body = original.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    os.replace(temp_path, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-root", type=Path, default=None)
    parser.add_argument("--until", default="2026-06-24", help="文章日期上限（不含）")
    parser.add_argument("--min-score", type=float, default=6.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--article-id", default=None, help="只处理单篇（调试）")
    parser.add_argument("--write", action="store_true", help="真写；默认 dry-run")
    parser.add_argument("--force", action="store_true", help="已有图片描述也重跑")
    args = parser.parse_args(argv)

    kb_root = Path(args.kb_root) if args.kb_root else default_knowledge_base_root()
    until = date.fromisoformat(args.until)
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    rows = index.get("articles") if isinstance(index, dict) else index

    targets: list[tuple[dict[str, Any], Path, list[str]]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("column") != "普通":
            continue
        row_date = _parse_date(str(row.get("date", "")))
        score = _as_float(row.get("score"))
        if row_date is None or row_date >= until:
            continue
        if score is None or score < args.min_score:
            continue
        if args.article_id and str(row.get("id", "")) != args.article_id:
            continue
        path = _article_md_path(kb_root, row)
        if path is None:
            continue
        md_text = path.read_text(encoding="utf-8", errors="ignore")
        if "## 图片描述" in md_text:
            if not args.force:
                continue
        elif "利好度" in md_text or "共识度" in md_text:
            continue
        images = _frontmatter_images(md_text)
        existing = [kb_root / ref.lstrip("/") for ref in images]
        existing = [p for p in existing if p.is_file()]
        if existing:
            targets.append((row, path, [str(p) for p in existing]))

    targets.sort(key=lambda item: str(item[0].get("date", "")))
    if args.limit is not None:
        targets = targets[: args.limit]

    print(f"candidates={len(targets)} until={args.until} min_score={args.min_score}")
    if not args.write:
        for row, path, images in targets[:20]:
            print("DRY", str(row.get("date"))[:10], row.get("id"), path.name, images)
        if len(targets) > 20:
            print(f"... 其余 {len(targets) - 20} 篇略（dry-run）")
        return 0

    stats: Counter[str] = Counter()
    errors: list[str] = []
    name_map = load_a_share_map(kb_root)
    for row, path, images in targets:
        article_id = str(row.get("id", ""))
        parts: list[str] = []
        normalized = 0
        for image_path in images:
            provenance = describe_image_with_provenance(
                image_path, prompt=_SCORE_PROMPT
            )
            if not provenance.llm_desc or provenance.vision_provider == "none":
                errors.append(
                    f"{article_id} {Path(image_path).name}: "
                    f"{provenance.error or 'empty vision result'}"
                )
                continue
            filename = Path(image_path).name
            desc, desc_corrected = normalize_vision_text(
                provenance.llm_desc, name_map
            )
            normalized += desc_corrected
            parts.append(
                f"### {filename} (LLM · {provenance.vision_provider}/"
                f"{provenance.vision_model})\n\n"
                f"fallback_chain: {provenance.fallback_chain}\n\n"
                f"{desc.rstrip()}\n"
            )
        if not parts:
            stats["vision_failed"] += 1
            continue
        section = "\n## 图片描述\n\n" + "\n".join(parts)
        try:
            _write_image_desc_section(path, section)
            stats["written"] += 1
            print(
                "WROTE", article_id, path.name,
                f"images={len(parts)} normalized={normalized}",
            )
        except Exception as exc:  # noqa: BLE001 — 单篇失败不中断
            errors.append(f"{article_id}: {exc}")
            stats["write_error"] += 1

    print("stats:", dict(stats))
    if errors:
        print("errors:", len(errors))
        for error in errors[:10]:
            print("  -", error)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
