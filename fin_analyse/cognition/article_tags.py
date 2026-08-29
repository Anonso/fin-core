"""文章标签系统单缝模块（设计稿 v2 定稿：docs/design/article-tags.md）。

存储面（sidecar——永不回写文章文件、永不写 index.json）：
- ``<kb_root>/runtime/cognition/article_tags.jsonl``（目录 0700 / 文件 0600）；
- 行 = 一条打标事实：``{"article_id","tag","dim","source","rule_id",
  "tagged_at","action"}``；删标签写墓碑（action=remove），不物理删历史；
- 锁协议（v2 钉死，append 与 compaction 共用）：同一把目录 fd flock
  （目录 fd 天然抗 rename）。append = ``LOCK_EX|LOCK_NB`` + 有界重试
  （3 次 × 50ms），仍失败跳过打标 + warning（不阻塞 ingest）；
  compaction = 阻塞 ``LOCK_EX``（手动 CLI，v1 无定时任务）。禁止照搬
  ``_save_index`` 的无锁 os.replace 模式；
- 有效集（盲评 F8）：同 (article_id, tag) 的行按行序 last-write-wins；
  add 仅当有效集无该 tag，remove 仅当有。

维度（v2：三派生 + 两存储）：
- 存储维 ``content``（内容类型，关键词规则自动）/ ``manual``（手动 CLI）；
- 派生维（读时派生，不落存储，盲评 F9）：栏目（index column 归一化）、
  质量（F-06 标记 + 结尾省略号 + 路径解析）、深化（compact 目录有产物，
  单次目录枚举，不判新鲜——盲评 F10/F11）。

路径解析复用 ``cdp_scraper._safe_index_article_path`` 的语义（file→path
回退 + 目录圈定，盲评 F17），一致性由测试对着 scraper 实现逐分支比对。

已知取舍（v1）：手动 remove 一条自动 content 标后，reconciler 会按当前
规则重打（行序覆盖使墓碑失效）。content 维的最终修正路径是改规则表 +
``backfill --refresh``；rule_id 内容哈希保证 provenance 不撒谎（盲评 F13）。

「学习方法探讨」边界（owner 逐篇裁决，人工 CLI 为准）：
问学习/处理问题/用 AI/老师思路体系 = 属；问具体市场/标的观点、职业
咨询/职业规划、交易心理 QA = 不属；行情长帖附带 AI 学习附录按主体算。
（名单以 ``tags query --tag 学习方法探讨`` 为准，2026-08-29 首轮定态 3 篇。）
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_CONFIG_PATH = PROJECT_ROOT / "config" / "article_tag_rules.json"

TAGS_RELATIVE_PATH = Path("runtime") / "cognition" / "article_tags.jsonl"
COMPACT_DIR_RELATIVE = (
    Path("runtime") / "cognition" / "deep_read_artifacts" / "compact"
)

CONTENT_DIM = "content"
MANUAL_DIM = "manual"
_STORED_DIMS = (CONTENT_DIM, MANUAL_DIM)

#: append 侧有界重试（设计稿 v2：3 次 × 50ms，仍失败跳过 + warning）。
APPEND_LOCK_RETRIES = 3
APPEND_LOCK_RETRY_SECONDS = 0.05

MANUAL_TAG_MAX_CHARS = 24
MANUAL_TAG_MAX_PER_ARTICLE = 10

# ── 栏目归一化（盲评 F12：覆盖 index 实测全部词表） ──────────────────

_COLUMN_NORMALIZATION: dict[str, str] = {
    "星大派锐评": "锐评",
    "星大派特刊": "特刊",
    "星大派好问题": "好问题",
    "凤仙郡小故事": "小故事",
    "问题回答": "问答",
    "回答问题": "问答",
    "普通": "普通",
    "重中之重": "重中之重",
    "大锅饭的宏观思考": "宏观思考",
    "版本强势英雄": "其他(游戏栏)",
}


def normalize_column(raw: str) -> str:
    """index column → 归一化名；未知 → ``其他:<原名>``。"""
    name = str(raw or "").strip()
    return _COLUMN_NORMALIZATION.get(name, f"其他:{name}")


# ── 路径解析（复用 scraper 缝语义，F17） ────────────────────────────


def safe_index_article_path(kb_root: Path, entry: Mapping[str, Any]) -> Path | None:
    """Resolve one index article strictly below ``<kb_root>/articles``.

    与 ``CdpBridgeScraper._safe_index_article_path`` 逐分支同语义：
    file 字段只接受裸文件名；否则回退 path 字段（绝对直用、相对接
    kb_root）；最后做目录圈定（resolve 后必须仍在 articles 下）。
    621 条无 file 字段的存量条目依赖这条回退路径。
    """
    articles_dir = Path(kb_root) / "articles"
    candidate: Path | None = None
    article_file = entry.get("file")
    if isinstance(article_file, str):
        normalized_file = article_file.strip()
        file_path = Path(normalized_file)
        if (
            normalized_file
            and normalized_file not in {".", ".."}
            and not file_path.is_absolute()
            and file_path.name == normalized_file
        ):
            candidate = articles_dir / normalized_file

    if candidate is None:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        candidate = Path(raw_path.strip())
        if not candidate.is_absolute():
            candidate = Path(kb_root) / candidate
    try:
        candidate.resolve(strict=False).relative_to(articles_dir.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return candidate


# ── index 读取（以 articles 列表为准，不用 total 字段） ─────────────


def read_index_articles(kb_root: Path) -> list[dict[str, Any]]:
    """Read the index ``articles`` list; missing/corrupt index → empty list."""
    try:
        payload = json.loads((Path(kb_root) / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        return []
    return [dict(entry) for entry in articles if isinstance(entry, Mapping)]


# ── 行与有效集 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TagRow:
    """One append-only tagging fact."""

    article_id: str
    tag: str
    dim: str
    source: str
    rule_id: str
    tagged_at: str
    action: str  # "add" | "remove"
    fallback: bool = False

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "article_id": self.article_id,
            "tag": self.tag,
            "dim": self.dim,
            "source": self.source,
            "rule_id": self.rule_id,
            "tagged_at": self.tagged_at,
            "action": self.action,
        }
        if self.fallback:
            payload["fallback"] = True
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TagRow | None:
        """Strict decode; any shape violation → None (counted as torn line)."""
        article_id = data.get("article_id")
        tag = data.get("tag")
        dim = data.get("dim")
        action = data.get("action")
        if not all(isinstance(v, str) and v for v in (article_id, tag, dim, action)):
            return None
        if action not in {"add", "remove"}:
            return None
        tagged_at = data.get("tagged_at")
        return cls(
            article_id=str(article_id),
            tag=str(tag),
            dim=str(dim),
            source=str(data.get("source", "")),
            rule_id=str(data.get("rule_id", "")),
            tagged_at=str(tagged_at) if isinstance(tagged_at, str) else "",
            action=str(action),
            fallback=bool(data.get("fallback", False)),
        )


class TagLockBusyError(RuntimeError):
    """Bounded non-blocking append could not take the directory flock."""


@dataclass(frozen=True)
class TagWriteResult:
    """Outcome of one tagging intent; ``status`` is machine-readable."""

    article_id: str
    tag: str
    status: str  # added | skipped_present | skipped_absent | lock_busy | invalid
    detail: str = ""


@dataclass(frozen=True)
class CompactionResult:
    rows_kept: int
    torn_dropped: int


class TagStore:
    """append-only jsonl sidecar + directory-fd flock + effective sets."""

    def __init__(self, kb_root: Path) -> None:
        self._kb_root = Path(kb_root)
        self._path = self._kb_root / TAGS_RELATIVE_PATH

    @property
    def path(self) -> Path:
        return self._path

    # ── 读 ──────────────────────────────────────────────────────

    def read_rows(self) -> tuple[list[TagRow], int]:
        """All valid rows in file order, plus the torn-line count."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], 0
        except OSError as exc:
            logger.warning("article tags unreadable: %s", type(exc).__name__)
            return [], 0
        rows: list[TagRow] = []
        torn = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                torn += 1
                continue
            row = TagRow.from_mapping(payload) if isinstance(payload, Mapping) else None
            if row is None:
                torn += 1
            else:
                rows.append(row)
        return rows, torn

    def effective_wins(self) -> dict[tuple[str, str], TagRow]:
        """Last row per (article_id, tag) — the last-write-wins overlay."""
        wins: dict[tuple[str, str], TagRow] = {}
        rows, _ = self.read_rows()
        for row in rows:
            wins[(row.article_id, row.tag)] = row
        return wins

    def effective_tags(self, article_id: str) -> dict[str, list[str]]:
        """Currently effective tags of one article, grouped by dim."""
        grouped: dict[str, list[str]] = {dim: [] for dim in _STORED_DIMS}
        for (aid, _tag), row in self.effective_wins().items():
            if aid == article_id and row.action == "add":
                grouped.setdefault(row.dim, []).append(row.tag)
        return grouped

    # ── 写（append + 锁） ───────────────────────────────────────

    def tag_article(
        self,
        article_id: str,
        tag: str,
        *,
        dim: str,
        source: str,
        rule_id: str = "",
        fallback: bool = False,
        now: datetime | None = None,
        supersede_rule_ids: frozenset[str] = frozenset(),
    ) -> TagWriteResult:
        """Admit-gated append of one add row (盲评 F8 有效集 admit).

        ``supersede_rule_ids`` 仅供 reconciler refresh：有效行带旧 rule_id
        时允许追加新行按行序覆盖（设计稿 §3「旧行被新行覆盖」），否则
        admit 照常拒绝。
        """
        article_id = article_id.strip()
        tag = tag.strip()
        if not article_id or not tag:
            return TagWriteResult(article_id, tag, "invalid", "empty id or tag")
        if dim not in _STORED_DIMS:
            return TagWriteResult(article_id, tag, "invalid", f"unknown dim: {dim}")
        if dim == MANUAL_DIM:
            invalid = _validate_manual_tag(article_id, tag, self)
            if invalid is not None:
                return TagWriteResult(article_id, tag, "invalid", invalid)
        elif not rule_id:
            return TagWriteResult(article_id, tag, "invalid", "content dim requires rule_id")
        effective = self.effective_tags(article_id)
        merged = {existing for tags in effective.values() for existing in tags}
        if tag in merged:
            winner = self.effective_wins().get((article_id, tag))
            supersede = (
                dim == CONTENT_DIM
                and winner is not None
                and winner.dim == CONTENT_DIM
                and winner.rule_id in supersede_rule_ids
            )
            if not supersede:
                return TagWriteResult(article_id, tag, "skipped_present")
        row = TagRow(
            article_id=article_id,
            tag=tag,
            dim=dim,
            source=source,
            rule_id=rule_id,
            tagged_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
            action="add",
            fallback=fallback,
        )
        return self._append_guarded(row)

    def remove_tag(
        self,
        article_id: str,
        tag: str,
        *,
        source: str = "manual",
        now: datetime | None = None,
    ) -> TagWriteResult:
        """Admit-gated tombstone; dim carries the overridden row's dim."""
        article_id = article_id.strip()
        tag = tag.strip()
        if not article_id or not tag:
            return TagWriteResult(article_id, tag, "invalid", "empty id or tag")
        winner = self.effective_wins().get((article_id, tag))
        if winner is None or winner.action != "add":
            return TagWriteResult(article_id, tag, "skipped_absent")
        row = TagRow(
            article_id=article_id,
            tag=tag,
            dim=winner.dim,
            source=source,
            rule_id="",
            tagged_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
            action="remove",
        )
        return self._append_guarded(row)

    def _append_guarded(self, row: TagRow) -> TagWriteResult:
        """Bounded non-blocking append; busy → status, never raises (设计稿 §1)."""
        try:
            return self._append(row)
        except TagLockBusyError:
            return TagWriteResult(row.article_id, row.tag, "lock_busy")

    def _append(self, row: TagRow) -> TagWriteResult:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self._path.parent, 0o700)
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_fd = os.open(self._path.parent, dir_flags)
        try:
            for attempt in range(APPEND_LOCK_RETRIES):
                try:
                    fcntl.flock(dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if attempt + 1 >= APPEND_LOCK_RETRIES:
                        raise TagLockBusyError(
                            f"article tags lock busy after {APPEND_LOCK_RETRIES} tries"
                        ) from None
                    time.sleep(APPEND_LOCK_RETRY_SECONDS)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (row.to_json() + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        return TagWriteResult(row.article_id, row.tag, "added")

    # ── compaction（阻塞锁内 read-merge-replace） ────────────────

    def compact(self) -> CompactionResult:
        """Rewrite the jsonl keeping every valid row (tombstones included).

        只清 torn 行；墓碑不物理删历史。temp 0600 + fsync + os.replace +
        显式 fchmod 0600（不继承 existing_mode——生产 jsonl 实测 0664 的
        教训，盲评 F15）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self._path.parent, 0o700)
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_fd = os.open(self._path.parent, dir_flags)
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX)  # 阻塞：手动 CLI 触发
            rows, torn = self.read_rows()
            temp_path = self._path.with_name(
                f".{self._path.name}.{secrets.token_hex(8)}.tmp"
            )
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(row.to_json() + "\n")
                    handle.flush()
                    os.fchmod(handle.fileno(), 0o600)
                    os.fsync(handle.fileno())
            except BaseException:
                with suppress(OSError):
                    temp_path.unlink()
                raise
            os.replace(temp_path, self._path)
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return CompactionResult(rows_kept=len(rows), torn_dropped=torn)


def _validate_manual_tag(article_id: str, tag: str, store: TagStore) -> str | None:
    if len(tag) > MANUAL_TAG_MAX_CHARS:
        return f"manual tag exceeds {MANUAL_TAG_MAX_CHARS} chars"
    existing = store.effective_tags(article_id).get(MANUAL_DIM, [])
    if tag not in existing and len(existing) >= MANUAL_TAG_MAX_PER_ARTICLE:
        return f"manual tags reach {MANUAL_TAG_MAX_PER_ARTICLE} per article"
    return None


# ── 规则引擎（rule_id = 规则名 + 配置内容 sha256 前 8 位，盲评 F13） ──

BUILTIN_RULES: dict[str, Any] = {
    "name": "content_rules",
    "rules": [
        {"name": "qa_question", "tag": "提问", "is_qa": True},
        {
            "name": "greeting",
            "tag": "寒暄",
            "keywords": [
                "谢谢老师",
                "感谢老师",
                "感谢分享",
                "多谢分享",
                "辛苦了",
                "新年快乐",
                "春节快乐",
                "元旦快乐",
                "中秋快乐",
                "祝老师",
            ],
        },
        {
            # 召回辅助；人工 CLI 判定为准。分界：问学习/处理问题/用 AI/
            # 老师思路体系=属于；问具体市场/标的观点=不属于。
            "name": "methodology",
            "tag": "学习方法探讨",
            "keywords": [
                "怎么学",
                "如何学",
                "学习方法",
                "学习路径",
                "学习顺序",
                "该怎么学",
                "从哪学起",
                "知识体系",
                "能力圈",
                "怎么用AI",
                "怎么用 AI",
                "如何用AI",
                "如何用 AI",
                "用AI来",
                "用 AI 来",
                "deep research",
                "DeepResearch",
                "投研 Skill",
                "投研skill",
            ],
        },
        {
            "name": "data_notice",
            "tag": "数据公告",
            "keywords": [
                "统计局",
                "PMI",
                "CPI",
                "PPI",
                "社融",
                "金融数据",
                "海关总署",
                "经济数据",
                "出货量数据",
                "装机数据",
            ],
        },
        {
            "name": "original_view",
            "tag": "原创观点",
            "keywords": [
                "我认为",
                "我的判断",
                "我的看法",
                "关键不在",
                "真正值得",
                "不追高",
                "需要观察",
                "核心逻辑",
                "这里我说一下",
            ],
        },
        {
            # 兜底召回：老师日常帖多为券商研报摘要（冒烟期望 ≈ 普通非QA 的 86%）。
            "name": "research_summary",
            "tag": "研报总结",
            "keywords": [
                "研报",
                "券商",
                "评级",
                "目标价",
                "盈利预测",
                "业绩",
                "财报",
                "纪要",
                "调研",
                "产业链",
                "供需",
                "涨价",
                "产能",
                "订单",
                "出货",
                "景气",
                "算力",
                "半导体",
                "激光雷达",
                "机器人",
                "电池",
                "出海",
                "能量评分",
            ],
        },
    ],
}


def canonical_rules_bytes(table: Mapping[str, Any]) -> bytes:
    """Semantic-content bytes of a rules table (whitespace-insensitive)."""
    return (
        json.dumps(table, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class ContentRule:
    name: str
    tag: str
    keywords: tuple[str, ...] = ()
    is_qa: bool | None = None

    def matches(self, signals: Mapping[str, Any]) -> bool:
        if self.is_qa is not None and bool(signals.get("is_qa", False)) != self.is_qa:
            return False
        if not self.keywords:
            return self.is_qa is not None
        haystack = f"{signals.get('title', '')}\n{signals.get('content', '')}"
        return any(keyword in haystack for keyword in self.keywords)


@dataclass(frozen=True)
class RuleTable:
    name: str
    rules: tuple[ContentRule, ...]
    rule_id: str
    fallback: bool

    def match(self, signals: Mapping[str, Any]) -> str | None:
        """First-match-wins over ordered rules; None = 内容类型未命中。"""
        for rule in self.rules:
            if rule.matches(signals):
                return rule.tag
        return None


_FALLBACK_WARNED = False


def _warn_fallback_once(reason: str) -> None:
    global _FALLBACK_WARNED
    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        logger.warning(
            "article tag rules unavailable (%s); falling back to built-in table",
            reason,
        )


def _table_from_mapping(payload: Mapping[str, Any]) -> RuleTable | None:
    name = payload.get("name")
    raw_rules = payload.get("rules")
    if not isinstance(name, str) or not name or not isinstance(raw_rules, list):
        return None
    rules: list[ContentRule] = []
    for entry in raw_rules:
        if not isinstance(entry, Mapping):
            return None
        rule_name = entry.get("name")
        tag = entry.get("tag")
        if not isinstance(rule_name, str) or not rule_name:
            return None
        if not isinstance(tag, str) or not tag:
            return None
        raw_keywords = entry.get("keywords")
        if raw_keywords is None:
            raw_keywords = []
        if not isinstance(raw_keywords, list) or not all(
            isinstance(keyword, str) and keyword for keyword in raw_keywords
        ):
            return None
        is_qa = entry.get("is_qa")
        if is_qa is not None and not isinstance(is_qa, bool):
            return None
        if raw_keywords and is_qa is not None:
            return None
        rules.append(
            ContentRule(
                name=rule_name,
                tag=tag,
                keywords=tuple(raw_keywords),
                is_qa=is_qa,
            )
        )
    if not rules:
        return None
    return RuleTable(name=name, rules=tuple(rules), rule_id="", fallback=False)


def _builtin_table() -> RuleTable:
    table = _table_from_mapping(BUILTIN_RULES)
    assert table is not None  # built-in table is authored valid
    rule_id = _rule_id_for(table.name, canonical_rules_bytes(BUILTIN_RULES))
    return RuleTable(
        name=table.name, rules=table.rules, rule_id=rule_id, fallback=True
    )


def _rule_id_for(name: str, content: bytes) -> str:
    return f"{name}.{hashlib.sha256(content).hexdigest()[:8]}"


def load_rules(path: Path | None = None) -> RuleTable:
    """Load rules from config; missing/invalid → loud once + built-in fallback."""
    config_path = RULES_CONFIG_PATH if path is None else Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _warn_fallback_once(f"config missing: {config_path}")
        return _builtin_table()
    except (OSError, ValueError) as exc:
        _warn_fallback_once(f"config unreadable: {type(exc).__name__}: {exc}")
        return _builtin_table()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _warn_fallback_once(f"config invalid JSON: {exc}")
        return _builtin_table()
    if not isinstance(payload, Mapping):
        _warn_fallback_once("config is not an object")
        return _builtin_table()
    table = _table_from_mapping(payload)
    if table is None:
        _warn_fallback_once("config schema invalid")
        return _builtin_table()
    rule_id = _rule_id_for(table.name, canonical_rules_bytes(payload))
    return RuleTable(name=table.name, rules=table.rules, rule_id=rule_id, fallback=False)


def reset_fallback_warning() -> None:
    """Test seam: re-arm the once-per-process fallback warning."""
    global _FALLBACK_WARNED
    _FALLBACK_WARNED = False


# ── 信号采集与派生维度 ──────────────────────────────────────────────


def article_signals(kb_root: Path, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Title/content/is_qa/column signals for rule matching; None = 无正文可用。

    正文按 safe 缝解析；文件缺失时仍可用 index 元数据匹配（title-only），
    只有当 entry 本身无法定位（连 path 都没有）时才返回 None。
    """
    title = str(entry.get("title", ""))
    is_qa = entry.get("type") == "q&a" or entry.get("is_qa") is True
    content = ""
    path = safe_index_article_path(kb_root, entry)
    if path is not None and path.is_file():
        try:
            content = _article_body(path)
        except OSError:
            content = ""
    if not title and not content:
        return None
    return {"title": title, "content": content, "is_qa": is_qa}


def _article_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            return parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    return text


_INCOMPLETE_TRUE = {"true", "yes", "1"}
_TAIL_ELLIPSIS_RE = re.compile(r"(?:\.{3}|…)\s*$")

QUALITY_COMPLETE = "完整"
QUALITY_TRUNCATED = "截断"
QUALITY_MISSING = "文件缺失"
DEEPEN_ARTIFACT = "有产物"
DEEPEN_NO_ARTIFACT = "无产物"


def derive_quality(kb_root: Path, entry: Mapping[str, Any]) -> str:
    """F-06 标记 + 结尾省略号 + 路径解析 → 完整/截断/文件缺失。"""
    path = safe_index_article_path(kb_root, entry)
    if path is None or not path.is_file():
        return QUALITY_MISSING
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return QUALITY_MISSING
    body = text
    if text.startswith("---"):
        closing = text.find("\n---", 3)
        if closing != -1:
            frontmatter = text[3:closing]
            body = text[closing + 4 :]
            for line in frontmatter.splitlines():
                if line.strip().startswith("incomplete:"):
                    value = line.split(":", 1)[1].strip().lower()
                    if value in _INCOMPLETE_TRUE:
                        return QUALITY_TRUNCATED
                    break
    if _TAIL_ELLIPSIS_RE.search(body.rstrip()):
        return QUALITY_TRUNCATED
    return QUALITY_COMPLETE


# 与 DeepReadArtifactService._safe_artifact_key 同语义（紧凑文件命名）。
_SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_\-\.\:]+$")


def _safe_artifact_key(article_id: str) -> str:
    if not article_id or article_id in (".", ".."):
        return f"unsafe_{hashlib.sha256(article_id.encode()).hexdigest()[:16]}"
    if _SAFE_KEY_RE.match(article_id):
        return article_id
    return f"unsafe_{hashlib.sha256(article_id.encode()).hexdigest()[:16]}"


def deepen_map(kb_root: Path) -> dict[str, bool]:
    """单次目录枚举建 {article_id → compact 产物存在}（盲评 F10 约束）。"""
    compact_dir = Path(kb_root) / COMPACT_DIR_RELATIVE
    try:
        names = os.listdir(compact_dir)
    except OSError:
        return {}
    return {Path(name).stem: True for name in names if name.endswith(".json")}


def derive_deepen(artifacts: Mapping[str, bool], article_id: str) -> str:
    """语义 = 有产物，不判新鲜（盲评 F10/F11）。"""
    return DEEPEN_ARTIFACT if _safe_artifact_key(article_id) in artifacts else DEEPEN_NO_ARTIFACT


# ── 查询 ────────────────────────────────────────────────────────────


def _column_matches(entry_column: str, wanted: str) -> bool:
    """归一化相等，或入参本身就是归一化名/原始名。"""
    normalized = normalize_column(entry_column)
    return normalized == wanted or normalized == normalize_column(wanted)


def query(
    kb_root: Path,
    *,
    tag: str | None = None,
    column: str | None = None,
    quality: str | None = None,
) -> list[str]:
    """按有效标签/归一化栏目/质量过滤 index 文章，返回 article_id 列表。"""
    entries = read_index_articles(kb_root)
    if not entries:
        return []
    wins = TagStore(kb_root).effective_wins() if tag else None
    tag_ids = {
        aid
        for (aid, row_tag), row in (wins or {}).items()
        if row_tag == tag and row.action == "add"
    }
    result: list[str] = []
    for entry in entries:
        if column is not None and not _column_matches(str(entry.get("column", "")), column.strip()):
            continue
        if quality is not None and derive_quality(kb_root, entry) != quality:
            continue
        if tag is not None and str(entry.get("id", "")) not in tag_ids:
            continue
        result.append(str(entry.get("id", "")))
    return result


def describe_article(kb_root: Path, article_id: str) -> dict[str, Any] | None:
    """One article's stored + derived dims; None = id 不在 index。"""
    entry = next(
        (
            e
            for e in read_index_articles(kb_root)
            if str(e.get("id", "")) == article_id
        ),
        None,
    )
    if entry is None:
        return None
    stored = TagStore(kb_root).effective_tags(article_id)
    return {
        "article_id": article_id,
        "title": str(entry.get("title", "")),
        "tags": {dim: sorted(tags) for dim, tags in stored.items()},
        "column": normalize_column(str(entry.get("column", ""))),
        "quality": derive_quality(kb_root, entry),
        "deepen": derive_deepen(deepen_map(kb_root), article_id),
    }


# ── ingest 尾部钩子（poller 保存文章后；失败 warning 不阻塞） ────────


@dataclass(frozen=True)
class IngestTagReport:
    requested: int
    tagged: int
    already_tagged: int
    unmatchable: int
    lock_busy: int
    errors: int
    warnings: tuple[str, ...] = ()

    @property
    def incomplete(self) -> bool:
        return bool(self.lock_busy or self.errors or self.warnings)


def tag_saved_articles(
    saved_ids: Iterable[str],
    *,
    kb_root: Path,
    rules: RuleTable | None = None,
    store: TagStore | None = None,
    index_articles: list[dict[str, Any]] | None = None,
) -> IngestTagReport:
    """对本次 ingest 保存的 article_ids 打 content 维标。

    任何失败都折进计数/warnings，绝不 raise——打标缺口由 reconciler
    （``backfill``）闭合；锁取 LOCK_NB 有界重试，busy 只计数。
    """
    ids = [str(aid) for aid in saved_ids if str(aid or "").strip()]
    report_kwargs: dict[str, Any] = {
        "requested": len(ids),
        "tagged": 0,
        "already_tagged": 0,
        "unmatchable": 0,
        "lock_busy": 0,
        "errors": 0,
        "warnings": (),
    }
    if not ids:
        return IngestTagReport(**report_kwargs)
    warnings: list[str] = []
    table = rules if rules is not None else load_rules()
    entries = index_articles if index_articles is not None else read_index_articles(kb_root)
    if not entries:
        warnings.append("index_articles_unavailable")
        return IngestTagReport(warnings=tuple(warnings), **_minus(report_kwargs, "warnings"))
    by_id = {
        str(entry.get("id")): entry
        for entry in entries
        if entry.get("id")
    }
    store = store if store is not None else TagStore(kb_root)
    for article_id in ids:
        entry = by_id.get(article_id)
        if entry is None:
            warnings.append(f"index_entry_missing:{article_id}")
            report_kwargs["errors"] += 1
            continue
        signals = article_signals(kb_root, entry)
        if signals is None:
            warnings.append(f"signals_unavailable:{article_id}")
            report_kwargs["errors"] += 1
            continue
        tag = table.match(signals)
        if tag is None:
            report_kwargs["unmatchable"] += 1
            continue
        try:
            result = store.tag_article(
                article_id,
                tag,
                dim=CONTENT_DIM,
                source="auto",
                rule_id=table.rule_id,
                fallback=table.fallback,
            )
        except Exception as exc:  # noqa: BLE001 — 钩子绝不阻塞 ingest
            warnings.append(f"tag_error:{article_id}:{type(exc).__name__}")
            report_kwargs["errors"] += 1
            continue
        if result.status == "added":
            report_kwargs["tagged"] += 1
        elif result.status == "skipped_present":
            report_kwargs["already_tagged"] += 1
        elif result.status == "lock_busy":
            report_kwargs["lock_busy"] += 1
        else:
            warnings.append(f"tag_invalid:{article_id}:{result.detail}")
            report_kwargs["errors"] += 1
    return IngestTagReport(warnings=tuple(warnings), **_minus(report_kwargs, "warnings"))


def _minus(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in keys}


# ── reconciler（backfill 即 reconciler，可重入） ────────────────────


@dataclass(frozen=True)
class ReconcileReport:
    total_articles: int
    tagged: int
    already_tagged: int
    skipped_file_missing: int
    unmatchable: int
    lock_busy: int
    errors: int
    orphan_tag_rows: int
    orphan_files: int
    rule_id: str
    fallback_rules: bool
    refresh: bool
    dry_run: bool


def reconcile(
    kb_root: Path,
    *,
    dry_run: bool = False,
    refresh: bool = False,
    config_path: Path | None = None,
) -> ReconcileReport:
    """可重入回填：index 全量扫 content 维缺标的文章补标（盲评 F6/F7）。

    默认目标 = 无 content 有效标的文章；``refresh`` = 有效 content 行带旧
    rule_id（或 fallback 行）的文章按当前规则重打（旧自动行被新行按行序
    覆盖）。孤儿标签行只计数告警，不迁移（v1 政策）。
    """
    kb_root = Path(kb_root)
    table = load_rules(config_path)
    entries = read_index_articles(kb_root)
    by_id = {str(entry.get("id")): entry for entry in entries if entry.get("id")}
    store = TagStore(kb_root)
    wins = store.effective_wins()

    content_effective: dict[str, list[TagRow]] = {}
    for (aid, _tag), row in wins.items():
        if row.action == "add" and row.dim == CONTENT_DIM:
            content_effective.setdefault(aid, []).append(row)
    orphan_tag_rows = sum(1 for (aid, _tag) in wins if aid not in by_id)

    if refresh:
        target_ids = [
            aid
            for aid, rows in content_effective.items()
            if any(row.rule_id != table.rule_id or row.fallback for row in rows)
        ]
    else:
        target_ids = [aid for aid in by_id if aid not in content_effective]

    orphan_files = _count_orphan_files(kb_root, entries)

    report = {
        "total_articles": len(entries),
        "tagged": 0,
        "already_tagged": 0,
        "skipped_file_missing": 0,
        "unmatchable": 0,
        "lock_busy": 0,
        "errors": 0,
        "orphan_tag_rows": orphan_tag_rows,
        "orphan_files": orphan_files,
        "rule_id": table.rule_id,
        "fallback_rules": table.fallback,
        "refresh": refresh,
        "dry_run": dry_run,
    }
    for article_id in target_ids:
        entry = by_id[article_id]
        article_path = safe_index_article_path(kb_root, entry)
        if article_path is None or not article_path.is_file():
            # 文件缺失/无法定位：不猜，跳过 + 计数（质量维会显式标注）。
            report["skipped_file_missing"] += 1
            continue
        signals = article_signals(kb_root, entry)
        if signals is None:
            report["skipped_file_missing"] += 1
            continue
        tag = table.match(signals)
        if tag is None:
            report["unmatchable"] += 1
            continue
        if dry_run:
            report["tagged"] += 1
            continue
        supersede = frozenset(
            row.rule_id
            for row in content_effective.get(article_id, [])
            if row.rule_id != table.rule_id
        )
        try:
            result = store.tag_article(
                article_id,
                tag,
                dim=CONTENT_DIM,
                source="auto",
                rule_id=table.rule_id,
                fallback=table.fallback,
                supersede_rule_ids=supersede,
            )
        except Exception as exc:  # noqa: BLE001 — 回填逐篇容错
            logger.warning("reconcile tag failed for %s: %s", article_id, exc)
            report["errors"] += 1
            continue
        if result.status == "added":
            report["tagged"] += 1
        elif result.status == "skipped_present":
            report["already_tagged"] += 1
        elif result.status == "lock_busy":
            report["lock_busy"] += 1
        else:
            report["errors"] += 1
    return ReconcileReport(**report)


def _count_orphan_files(kb_root: Path, entries: list[dict[str, Any]]) -> int:
    """.md on disk but referenced by no index entry（跳过 + 计数告警）。"""
    articles_dir = Path(kb_root) / "articles"
    try:
        disk_names = {
            name for name in os.listdir(articles_dir) if name.endswith(".md")
        }
    except OSError:
        return 0
    referenced: set[str] = set()
    for entry in entries:
        path = safe_index_article_path(kb_root, entry)
        if path is not None:
            referenced.add(path.name)
    return len(disk_names - referenced)


# ── CLI（fin-cognition tags …） ─────────────────────────────────────


def _resolve_kb_root(value: str | None) -> Path:
    if value is None:
        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        return default_knowledge_base_root()
    return Path(value)


@click.group("tags")
def tags_cli() -> None:
    """文章标签：sidecar 打标、查询、压缩、回填。"""


@tags_cli.command("add")
@click.argument("article_id")
@click.argument("tag")
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_add(article_id: str, tag: str, kb_root: str | None) -> None:
    """手动打标（manual 维，≤24 字，≤10 个/篇）。"""
    result = TagStore(_resolve_kb_root(kb_root)).tag_article(
        article_id, tag, dim=MANUAL_DIM, source="manual"
    )
    click.echo(f"{result.article_id} {result.tag} -> {result.status} {result.detail}".rstrip())
    if result.status in {"lock_busy", "invalid"}:
        raise SystemExit(1)


@tags_cli.command("remove")
@click.argument("article_id")
@click.argument("tag")
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_remove(article_id: str, tag: str, kb_root: str | None) -> None:
    """删标签（墓碑，不物理删历史）。"""
    result = TagStore(_resolve_kb_root(kb_root)).remove_tag(article_id, tag)
    click.echo(f"{result.article_id} {result.tag} -> {result.status} {result.detail}".rstrip())
    if result.status in {"lock_busy", "invalid"}:
        raise SystemExit(1)


@tags_cli.command("list")
@click.argument("article_id", required=False)
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_list(article_id: str | None, kb_root: str | None) -> None:
    """看一篇的全部维度；不带 ID 时输出全库标签计数。"""
    root = _resolve_kb_root(kb_root)
    if article_id:
        info = describe_article(root, article_id)
        if info is None:
            click.echo(f"article not in index: {article_id}")
            raise SystemExit(1)
        click.echo(json.dumps(info, ensure_ascii=False, indent=2))
        return
    wins = TagStore(root).effective_wins()
    counts: dict[str, int] = {}
    for (_aid, tag_value), row in wins.items():
        if row.action != "add":
            continue
        key = f"{row.dim}:{tag_value}"
        counts[key] = counts.get(key, 0) + 1
    for key in sorted(counts):
        click.echo(f"{key}\t{counts[key]}")


@tags_cli.command("query")
@click.option("--tag", default=None, help="按有效标签过滤（存储维）")
@click.option("--column", default=None, help="按归一化栏目过滤")
@click.option(
    "--quality",
    type=click.Choice([QUALITY_COMPLETE, QUALITY_TRUNCATED, QUALITY_MISSING]),
    default=None,
    help="按派生质量过滤",
)
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_query(tag: str | None, column: str | None, quality: str | None, kb_root: str | None) -> None:
    """查询：tags query --tag 研报总结 --column 普通 --quality 完整。"""
    ids = query(_resolve_kb_root(kb_root), tag=tag, column=column, quality=quality)
    for article_id in ids:
        click.echo(article_id)
    filters = ",".join(
        f"{name}={value}"
        for name, value in (("tag", tag), ("column", column), ("quality", quality))
        if value
    )
    click.echo(f"# {len(ids)} articles" + (f" [{filters}]" if filters else " [no filter]"))


@tags_cli.command("compact")
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_compact(kb_root: str | None) -> None:
    """压缩 sidecar（阻塞锁内 read-merge-replace，保墓碑）。"""
    result = TagStore(_resolve_kb_root(kb_root)).compact()
    click.echo(f"rows_kept={result.rows_kept} torn_dropped={result.torn_dropped}")


@tags_cli.command("backfill")
@click.option("--dry-run", is_flag=True, help="只统计不写入")
@click.option("--refresh", is_flag=True, help="对旧 rule_id 行所在文章按当前规则重打")
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
def tags_backfill(
    dry_run: bool, refresh: bool, config_path: str | None, kb_root: str | None
) -> None:
    """可重入 reconciler：补齐 content 维缺标的文章。"""
    report = reconcile(
        _resolve_kb_root(kb_root),
        dry_run=dry_run,
        refresh=refresh,
        config_path=Path(config_path) if config_path else None,
    )
    click.echo(
        json.dumps(
            {
                "total_articles": report.total_articles,
                "tagged": report.tagged,
                "already_tagged": report.already_tagged,
                "skipped_file_missing": report.skipped_file_missing,
                "unmatchable": report.unmatchable,
                "lock_busy": report.lock_busy,
                "errors": report.errors,
                "orphan_tag_rows": report.orphan_tag_rows,
                "orphan_files": report.orphan_files,
                "rule_id": report.rule_id,
                "fallback_rules": report.fallback_rules,
                "refresh": report.refresh,
                "dry_run": report.dry_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report.orphan_tag_rows:
        logger.warning("orphan tag rows (id drift, not migrated): %d", report.orphan_tag_rows)
    if report.orphan_files:
        logger.warning("orphan article files (not in index, skipped): %d", report.orphan_files)


if __name__ == "__main__":
    tags_cli()
