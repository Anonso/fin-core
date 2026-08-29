"""一次性数据手术（2026-08-30，thesis-core-rewrite-06 设计 C 节）。

范围（owner 2026-08-29 直接授权）：
  1. 清洗 legacy 脏文：确定性剥离嵌正文的评论串 / UI chrome / 点赞脚注；
  2. backfill：teacher 栏（凤仙郡小故事/星大派特刊/星大派锐评/星大派好问题）
     frontmatter 补 source_classification: teacher_original（与现行采集
     cdp_scraper.py:2896 写入值逐字一致）。

安全（硬边界4）：改写前 owner-only 备份（目录 0700/文件 0600）+ sha256
manifest（per-file before/after、ops、跳过原因）。幂等（P2-13）：已达
目标态的文件跳过；manifest 每次全量重建，记录终态与本轮 ops。

规则冻结（P2-10，设计门裁决 #10）：
  - CHROME_WORDS / CHROME_LINE_RE / HASHTAG_LINE_RE / CREPLY / CTIME 常量；
  - 密集跑判据：锚点后 400 字符内 ≥2 个其他评论标记 → 评论块起点截断；
  - 行级 chrome 剥离 + 尾部点赞脚注剥离（dry-run 2026-08-29：
    296 截断 / 113 脚注 / 947 净）；
  - 校验门：清洗后正文无任何冻结标记、≥150 字；不过者跳过不写并登记。

用法：
  python scripts/legacy_kb_surgery_20260830.py [--apply]   # 默认 dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

KB_ROOT = Path(
    os.environ.get(
        "FIN_KB_ROOT",
        "/home/ypk/.local/share/fin-analyse/shared/knowledge-base",
    )
)
STATE_DIR = Path(os.environ.get("FIN_STATE_DIR", "/home/ypk/.local/state/fin-analyse"))
SURGERY_DIR = STATE_DIR / "legacy-article-surgery-20260830"
BACKUP_DIR = SURGERY_DIR / "backup"

TEACHER_COLUMNS = frozenset(
    {"凤仙郡小故事", "星大派特刊", "星大派锐评", "星大派好问题"}
)
PROVENANCE_LINE = "source_classification: teacher_original"

# ── 冻结标记表（P2-10）───────────────────────────────────────────────────────
CHROME_WORDS = (
    "展开全部",
    "人觉得很赞",
    "最后编辑：",
    "扫码加入星球",
    "查看更多优质内容",
    "登录网页版",
    "下载知识星球",
)
CREPLY = re.compile(r"[^\s]{1,24}\s*回复\s*[^\s]{0,24}[：:]")
CTIME = re.compile(r"[^\s]{1,16}\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
CHROME_LINE_RE = re.compile(
    r"^(收费公示|企业认证|星球榜单|发现星球|登录网页版|运营高品质社群|"
    r"连接一千位铁杆粉丝|下载知识星球|内容创作、知识付费更方便|"
    r"支持的系统版本：|iOS \d+|Android [\d.]+|发表主题，随时捕捉记录身边灵感|"
    r"相比于在公众号几千字文章|创建付费星球|一分钟轻松创建付费星球|"
    r"收款\d+日后点击提现秒到账|笔记|管理后台|榜单|所有星球 · 最新动态|"
    r"创建/管理的星球|加入的星球|更多优质星球：|畅销榜|实力榜|续期榜|"
    r"更多优质内容|知识星球|展开全部|收起|查看详情|为我总结|"
    r"\d+人?觉得很赞?)$"
)
HASHTAG_LINE_RE = re.compile(r"^(?:#[^\s#]+\s*){2,}$")
LIKE_FOOTER_RE = re.compile(r"(?:等\d+人)?觉得很赞?")
DENSE_WINDOW = 400
DENSE_THRESHOLD = 2
MIN_CLEAN_BODY_CHARS = 150

FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _frontmatter_column(head: str) -> str:
    m = re.search(r"^column:\s*(.*)$", head, re.M)
    return m.group(1).strip() if m else ""


def _has_provenance(head: str) -> bool:
    return re.search(r"^source_classification:", head, re.M) is not None


def _body_markers(body: str) -> list[int]:
    """全部评论/chrome 锚点位置（去重升序）。"""
    out: set[int] = set()
    for word in CHROME_WORDS:
        idx = body.find(word)
        if idx >= 0:
            out.add(idx)
    for pattern in (CREPLY, CTIME):
        m = pattern.search(body)
        if m:
            out.add(m.start())
    return sorted(out)


def _dense_cut(body: str) -> int:
    """评论块起点：某锚点后 DENSE_WINDOW 字符内还有 ≥DENSE_THRESHOLD 个
    其他评论标记（密集跑）。返回 -1 表示无评论块。"""
    for i in _body_markers(body):
        win = body[i + 1 : i + 1 + DENSE_WINDOW]
        n = (
            sum(win.count(w) for w in ("觉得很赞", "回复", "最后编辑："))
            + len(CTIME.findall(win))
        )
        if n >= DENSE_THRESHOLD:
            return i
    return -1


def _strip_chrome_lines(body: str) -> str:
    kept = [
        line
        for line in body.splitlines()
        if not CHROME_LINE_RE.match(line.strip())
        and not HASHTAG_LINE_RE.match(line.strip())
    ]
    return "\n".join(kept).strip() + "\n" if kept else ""


def clean_body(body: str) -> tuple[str, str]:
    """返回 (cleaned, op)。op ∈ cut|footer|tail|none。"""
    cut = _dense_cut(body)
    if cut >= 0:
        return _strip_chrome_lines(body[:cut]), "cut"
    markers = _body_markers(body)
    if markers:
        # 尾区规则：无密集跑但标记全落正文末 20% → 尾部 chrome，截断。
        if markers[0] >= 0.8 * len(body):
            return _strip_chrome_lines(body[: markers[0]]), "tail"
        # 多为尾部点赞脚注（等N人觉得很赞）——仅剥脚注不截断。
        cleaned = LIKE_FOOTER_RE.sub("", body)
        cleaned = _strip_chrome_lines(cleaned)
        return cleaned, "footer"
    return body, "none"


def process(apply: bool) -> dict:
    articles = sorted((KB_ROOT / "articles").glob("*.md"))
    manifest: dict[str, object] = {
        "surgery": "legacy-article-surgery-20260830",
        "kb_root": str(KB_ROOT),
        "applied": apply,
        "frozen_rules": {
            "chrome_words": list(CHROME_WORDS),
            "dense_window": DENSE_WINDOW,
            "dense_threshold": DENSE_THRESHOLD,
            "min_clean_body_chars": MIN_CLEAN_BODY_CHARS,
        },
        "counts": {},
        "files": [],
    }
    counts = {"clean_cut": 0, "clean_footer": 0, "clean_skipped": 0,
              "backfill": 0, "untouched": 0}
    files: list[dict] = []

    SURGERY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SURGERY_DIR, 0o700)
    if apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)

    for path in articles:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            counts["clean_skipped"] += 1
            files.append({"file": path.name, "op": "skip_no_frontmatter"})
            continue
        head, body = text[: m.end()], text[m.end() :]
        entry: dict = {"file": path.name, "sha256_before": _sha256(raw)}

        ops: list[str] = []
        new_body, op = clean_body(body)
        if op != "none":
            if len(new_body.strip()) < MIN_CLEAN_BODY_CHARS:
                counts["clean_skipped"] += 1
                entry["op"] = f"skip_short_after_{op}"
            elif _body_markers(_strip_chrome_lines(new_body)):
                counts["clean_skipped"] += 1
                entry["op"] = f"skip_residual_markers_after_{op}"
            else:
                body = new_body
                ops.append(f"clean_{op}")

        column = _frontmatter_column(head)
        if column in TEACHER_COLUMNS and not _has_provenance(head):
            # 插在 column 行后，与现行采集的 frontmatter 顺序一致。
            head = re.sub(
                r"^(column:.*)$",
                rf"\1\n{PROVENANCE_LINE}",
                head,
                count=1,
                flags=re.M,
            )
            ops.append("backfill")

        if not ops:
            counts["untouched"] += 1
            if "op" in entry:
                # 仅清洗被跳过、无 backfill：登记跳过原因但不写盘。
                files.append(entry)
            continue
        for tag in ops:
            counts[tag] = counts.get(tag, 0) + 1
        entry["ops"] = ops
        new_text = head + body
        entry["sha256_after"] = _sha256(new_text.encode("utf-8"))
        entry["bytes_before"] = len(raw)
        entry["bytes_after"] = len(new_text.encode("utf-8"))
        files.append(entry)

        if apply:
            backup = BACKUP_DIR / path.name
            if not backup.exists():
                shutil.copy2(path, backup)
                os.chmod(backup, 0o600)
            tmp = path.with_name(path.name + ".surgery-tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)

    manifest["counts"] = counts
    manifest["files"] = files
    out = SURGERY_DIR / ("manifest.json" if apply else "manifest.dry-run.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(out, 0o600)
    print(json.dumps(counts, ensure_ascii=False))
    print(f"manifest -> {out}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行写盘（默认 dry-run）")
    args = parser.parse_args(argv)
    if not KB_ROOT.is_dir():
        print(f"KB root not found: {KB_ROOT}", file=sys.stderr)
        return 2
    process(apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
