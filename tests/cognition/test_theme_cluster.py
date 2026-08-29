from fin_analyse.cognition.models import InformationUnit, UsagePolicy
from fin_analyse.cognition.theme_cluster import assign_theme_clusters


def _unit(unit_id: str, title: str, thesis: str, topics: list[str]) -> InformationUnit:
    return InformationUnit(
        unit_id=unit_id,
        source_id=f"src-{unit_id}",
        teacher_id="guo",
        unit_type="strategic_thesis",
        title=title,
        thesis=thesis,
        original_evidence=[thesis],
        apprentice_interpretation="推演",
        confidence=0.8,
        related_companies=[],
        related_topics=topics,
        theme_cluster_ids=[],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-24T00:00:00",
        metadata={},
    )


def test_assigns_semiconductor_material_cluster():
    units = [
        _unit("u1", "钼前驱体", "钼前驱体是半导体材料卡口。", ["钼前驱体", "半导体"]),
        _unit("u2", "去日化", "半导体设备材料零部件去日化。", ["去日化", "半导体"]),
    ]

    assigned, clusters = assign_theme_clusters(units, now="2026-06-24T00:00:00")

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_id == "cluster-semi-materials-dejapanization"
    assert cluster.active_status == "reinforced"
    assert set(cluster.unit_ids) == {"u1", "u2"}
    assert all(unit.theme_cluster_ids == [cluster.cluster_id] for unit in assigned)


def test_unrelated_unit_gets_general_cluster():
    units = [_unit("u1", "旅游", "六月旅游是市场纪律。", ["市场节奏"])]

    assigned, clusters = assign_theme_clusters(units, now="2026-06-24T00:00:00")

    assert len(clusters) == 1
    assert clusters[0].cluster_id == "cluster-market-discipline"
    assert assigned[0].theme_cluster_ids == ["cluster-market-discipline"]
