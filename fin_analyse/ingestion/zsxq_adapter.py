"""ZSXQ markdown source adapter."""

import re
from pathlib import Path

from fin_analyse.scraper.config import KNOWN_COMPANIES
from fin_analyse.utils.markdown import parse_frontmatter

from .models import Evidence, ParseArtifact, RawDocument, SourceInfo

# 中文公司名后缀模式，用于静态名单无匹配时的兜底提取
_COMPANY_SUFFIX_RE = re.compile(
    r"([一-鿿]{2,4}"
    r"(?:科技|股份|集团|电子|材料|光电|技术|医药|生物"
    r"|汽车|新能源|银行|证券|保险|地产|钢铁|水泥|化工|食品|传媒|通信"
    r"|软件|半导体|装备|制造|电气|医疗|航空|航天|物流|快递|矿业|能源"
    r"|环保|水务|燃气|电力|港口|铁路|信息|智能|数据|互联|网络"
    r"|精密|光学|仪器|设备|机械|重工|轻工|纺织|服饰|家居|建材"
    r"|包装|印刷|文化|教育|旅游|酒店|餐饮|零售|超市|百货))"
)


class ZsxqMarkdownAdapter:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.articles_dir = self.root / "articles"

    @property
    def source_info(self) -> SourceInfo:
        return SourceInfo(
            source_id="zsxq",
            name="知识星球",
            source_type="paid_community",
            reliability=0.75,
            freshness_policy="article_default",
        )

    def fetch(self, since: object | None = None) -> list[RawDocument]:
        docs = []
        for path in sorted(self.articles_dir.glob("*.md")):
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            external_id = meta.get("id") or path.stem
            title = extract_title(body)
            content = extract_content(body)
            metadata = dict(meta)
            metadata["path"] = str(path)
            metadata["score"] = parse_score(metadata.get("score"))
            companies = parse_list(metadata.get("companies"))
            metadata["companies"] = companies or match_known_companies(content)
            metadata["images"] = parse_list(metadata.get("images"))
            metadata["tags"] = parse_list(metadata.get("tags"))
            docs.append(
                RawDocument(
                    source_id="zsxq",
                    external_id=external_id,
                    title=title,
                    content=content,
                    metadata=metadata,
                )
            )
        return docs

    def parse(self, document: RawDocument) -> list[ParseArtifact]:
        return [
            ParseArtifact(
                artifact_id=f"{document.document_id}:metadata",
                source_id=document.source_id,
                document_id=document.document_id,
                artifact_type="metadata",
                content=str(document.metadata),
                metadata=document.metadata,
            ),
            ParseArtifact(
                artifact_id=f"{document.document_id}:text",
                source_id=document.source_id,
                document_id=document.document_id,
                artifact_type="text",
                content=document.content,
                metadata={"title": document.title},
            ),
        ]

    def extract_evidence(self, document: RawDocument) -> list[Evidence]:
        content = document.content.strip()
        if not content:
            return []
        return [
            Evidence(
                evidence_id=f"{document.document_id}:text:0",
                source_id=document.source_id,
                document_id=document.document_id,
                evidence_type="text_chunk",
                content=content,
                metadata={
                    "title": document.title,
                    "chunk_index": 0,
                    "companies": document.metadata.get("companies", []),
                    "tags": document.metadata.get("tags", []),
                    "images": document.metadata.get("images", []),
                    "score": document.metadata.get("score"),
                },
            )
        ]


def extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "..."


def extract_content(body: str) -> str:
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:]).strip()
    return body.strip()


def parse_list(value: object) -> list[str]:
    """Normalize frontmatter list field (yaml-parsed list or legacy string)."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            return [item.strip() for item in inner.split(",") if item.strip()] if inner else []
    return []


def match_known_companies(content: str) -> list[str]:
    known = [name for name in KNOWN_COMPANIES if name in content]
    if known:
        return known
    # 静态名单无匹配时，用常见公司名后缀正则兜底提取
    matches = _COMPANY_SUFFIX_RE.findall(content)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def parse_score(value: object) -> float | None:
    """Normalize frontmatter score field (yaml-parsed float or legacy string)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value not in {"", "None", "null"}:
        try:
            return float(value)
        except ValueError:
            return None
    return None
