"""Theme clustering for ZSXQ cognition units."""

from __future__ import annotations

from dataclasses import replace

from fin_analyse.cognition.models import InformationUnit, ThemeCluster

_SEMI_KEYWORDS = (
    "半导体",
    "钼",
    "钨",
    "WF6",
    "前驱体",
    "特气",
    "稀土",
    "去日化",
    "设备",
    "材料",
    "零部件",
)
_TIMING_KEYWORDS = ("旅游", "不要上头", "别急", "拿住", "市场节奏", "风险刹车")


def _matches(unit: InformationUnit, keywords: tuple[str, ...]) -> bool:
    haystack = " ".join([unit.title, unit.thesis, *unit.related_topics])
    return any(keyword in haystack for keyword in keywords)


def assign_theme_clusters(
    units: list[InformationUnit],
    *,
    now: str,
) -> tuple[list[InformationUnit], list[ThemeCluster]]:
    cluster_by_id: dict[str, ThemeCluster] = {}
    assigned: list[InformationUnit] = []

    for unit in units:
        if _matches(unit, _SEMI_KEYWORDS):
            cluster_id = "cluster-semi-materials-dejapanization"
            name = "半导体底层卡口 / AI硬科技材料 / 去日化"
            description = "星大派连续强化的半导体材料、设备零部件与去日化主题簇。"
            indicators = ["公告", "订单", "涨价", "产能", "客户认证", "出口管制"]
            risks = ["股价透支", "公司澄清", "认证周期过长", "替代技术证伪"]
        elif _matches(unit, _TIMING_KEYWORDS):
            cluster_id = "cluster-market-discipline"
            name = "市场节奏 / 风险纪律"
            description = "星大派锐评中关于不要上头、已有拿住、没有别急、旅游等节奏纪律。"
            indicators = ["老师后续锐评", "市场波动", "成交情绪"]
            risks = ["事件窗口过期", "老师观点修正"]
        else:
            cluster_id = "cluster-general-zsxq-cognition"
            name = "星大派一般认知线索"
            description = "尚未归并到明确主题簇的星大派认知单元。"
            indicators = ["后续提及"]
            risks = ["缺少强化"]

        assigned.append(replace(unit, theme_cluster_ids=[cluster_id]))
        existing = cluster_by_id.get(cluster_id)
        if existing is None:
            cluster_by_id[cluster_id] = ThemeCluster(
                cluster_id=cluster_id,
                name=name,
                description=description,
                teacher_id=unit.teacher_id,
                unit_ids=[unit.unit_id],
                source_ids=[unit.source_id],
                core_theses=[unit.thesis],
                active_status="new",
                priority=unit.confidence,
                last_reinforced_at=now,
                tracking_indicators=indicators,
                risks=risks,
                metadata={},
            )
        else:
            unit_ids = [*existing.unit_ids, unit.unit_id]
            source_ids = sorted({*existing.source_ids, unit.source_id})
            core_theses = [*existing.core_theses, unit.thesis]
            cluster_by_id[cluster_id] = replace(
                existing,
                unit_ids=unit_ids,
                source_ids=source_ids,
                core_theses=core_theses,
                active_status="reinforced" if len(unit_ids) > 1 else existing.active_status,
                priority=max(existing.priority, unit.confidence),
                last_reinforced_at=now,
            )

    return assigned, list(cluster_by_id.values())
