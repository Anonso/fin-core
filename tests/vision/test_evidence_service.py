"""Tests for VisionEvidenceService — extract visual evidence from image records."""

from datetime import UTC, datetime, timedelta

from fin_analyse.cognition.llm import CognitionCompletionControl
from fin_analyse.common.execution_control import ExecutionFence
from fin_analyse.vision.evidence import (
    VisionEvidenceRequest,
    VisionEvidenceResult,
    VisionEvidenceService,
    VisionFact,
    VisionImageEvidence,
)

# ---------------------------------------------------------------------------
# Fake LLM backend for testing
# ---------------------------------------------------------------------------


class _FakeLLMBackend:
    """Returns fixed JSON for visual fact extraction."""

    def __init__(self, facts: list[dict] | None = None):
        self._facts = facts or [
            {
                "fact": "涂胶显影设备 TEL 占 95% 市场份额",
                "companies": ["TEL", "芯源微"],
                "metrics": {"market_share": "95%", "segment": "涂胶显影"},
                "confidence": 0.9,
                "image_ref": "img_1",
            },
        ]
        self.last_prompt: str = ""

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        import json

        return json.dumps({"facts": self._facts}, ensure_ascii=False)


class _BoundedLLMBackend(_FakeLLMBackend):
    def __init__(self) -> None:
        super().__init__()
        self.bounded_calls = 0

    def complete(self, _prompt: str) -> str:
        raise AssertionError("controlled vision used unbounded complete")

    def complete_bounded(self, prompt: str, **_kwargs: object) -> str:
        self.bounded_calls += 1
        return super().complete(prompt)


class _FailingLLMBackend:
    """Simulates an LLM backend that always returns garbage."""

    def complete(self, prompt: str) -> str:
        return "not json at all"


class _RaisingLLMBackend:
    """LLM backend whose complete() always raises."""

    def complete(self, prompt: str) -> str:
        raise RuntimeError("vision LLM boom")


class _EmptyLLMBackend:
    """LLM backend that returns a blank response."""

    def complete(self, prompt: str) -> str:
        return "   "


class _NonDictLLMBackend:
    """LLM backend that returns valid JSON that is not a dict."""

    def complete(self, prompt: str) -> str:
        return "[1, 2, 3]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_image_records(
    descriptions: list[str] | None = None,
    ocr_texts: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> list[dict]:
    """Build legacy test-shaped image records (file/llm_description/ocr_text keys)."""
    records: list[dict] = []
    descs = descriptions or []
    ocrs = ocr_texts or []
    paths = file_paths or []
    max_len = max(len(descs), len(ocrs), len(paths), 1)
    for i in range(max_len):
        rec: dict = {}
        if i < len(paths):
            rec["file"] = paths[i]
        if i < len(descs):
            rec["llm_description"] = descs[i]
        if i < len(ocrs):
            rec["ocr_text"] = ocrs[i]
        records.append(rec)
    return records


def test_extract_threads_optional_bounded_control():
    backend = _BoundedLLMBackend()
    service = VisionEvidenceService(llm_backend=backend)
    control = CognitionCompletionControl(
        fence=ExecutionFence(datetime.now(UTC) + timedelta(minutes=1)),
        checkpoint=lambda: None,
    )

    result = service.extract(
        VisionEvidenceRequest(
            article_id="bounded-vision",
            image_descriptions=["图中显示 TEL 市占率 95%"],
        ),
        control=control,
    )

    assert result.visual_facts
    assert backend.bounded_calls == 1


def _make_cdp_image_records() -> list[dict]:
    """Build real CdpBridgeScraper._process_images-shaped records.

    These use the exact keys produced by _process_images:
    filename, path, ocr_text, llm_desc, vision_provider, vision_model,
    fallback_chain, error.
    """
    return [
        {
            "filename": "000.jpg",
            "path": "images/post_001/000.jpg",
            "ocr_text": "涂胶显影设备 TEL 95% 市场份额",
            "llm_desc": "图片显示涂胶显影设备市场格局，TEL占95%份额",
            "vision_provider": "gpt5",
            "vision_model": "gpt-5.4",
            "fallback_chain": ["gpt5:ok"],
            "error": "",
        },
        {
            "filename": "001.png",
            "path": "images/post_001/001.png",
            "ocr_text": "",
            "llm_desc": "刻蚀设备竞争格局，TEL占30%，LAM占25%",
            "vision_provider": "siliconflow",
            "vision_model": "Qwen3-VL-32B-Instruct",
            "fallback_chain": ["gpt5:error:timeout", "siliconflow:ok"],
            "error": "",
        },
    ]


# ---------------------------------------------------------------------------
# Step 1: Empty inputs → status="empty", no_visual_inputs data_gap
# ---------------------------------------------------------------------------


def test_extract_empty_inputs_returns_empty_with_data_gap():
    """Empty image_descriptions + image_ocr → status=empty with no_visual_inputs."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-001",
        image_descriptions=[],
        image_ocr_texts=[],
    )
    result = service.extract(request)

    assert result.article_id == "test-001"
    assert result.status == "empty"
    assert result.images == []
    assert result.visual_facts == []
    assert result.company_recommendations == []
    assert "no_visual_inputs" in result.data_gaps
    assert result.source_boundary == "sensory"
    assert result.advisory_only is True
    assert result.trading_decision is False
    assert result.execution_allowed is False


def test_extract_none_inputs_treated_as_empty():
    """None image_descriptions and image_ocr_texts → status=empty."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-002",
        image_descriptions=None,
        image_ocr_texts=None,
    )
    result = service.extract(request)

    assert result.status == "empty"
    assert "no_visual_inputs" in result.data_gaps


# ---------------------------------------------------------------------------
# Step 2: Normalized image_records with OCR/LLM desc/provider metadata
# ---------------------------------------------------------------------------


def test_normalized_image_records_from_scraper_dicts():
    """Scraper-shaped dicts → normalized VisionImageEvidence list."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-003",
        image_descriptions=["钼前驱体总分14.5"],
        image_ocr_texts=["钼前驱体 14.5 WF6 已暴涨"],
        image_records=_make_image_records(
            descriptions=["钼前驱体总分14.5"],
            ocr_texts=["钼前驱体 14.5 WF6 已暴涨"],
            file_paths=["images/abc/001.png"],
        ),
    )
    result = service.extract(request)

    assert result.article_id == "test-003"
    assert result.status == "ok"
    assert len(result.images) == 1
    img = result.images[0]
    assert isinstance(img, VisionImageEvidence)
    assert img.file_path == "images/abc/001.png"
    assert img.llm_description == "钼前驱体总分14.5"
    assert img.ocr_text == "钼前驱体 14.5 WF6 已暴涨"

    # Provider summary should report image count
    assert "1 image" in result.provider_summary or "image" in result.provider_summary


def test_normalized_image_records_partial_fields():
    """Image records with only some fields filled normalize correctly."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-004",
        image_descriptions=["desc only"],
        image_ocr_texts=[],
        image_records=_make_image_records(descriptions=["desc only"]),
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert len(result.images) == 1
    img = result.images[0]
    assert img.llm_description == "desc only"
    assert img.ocr_text == ""


def test_image_descriptions_fallback_when_no_records():
    """When only image_descriptions are provided (no image_records), build evidence from them."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-005",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
        image_records=None,
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert len(result.images) >= 1
    # The descriptions should be reflected in the images
    desc_texts = [img.llm_description for img in result.images]
    assert any("TEL" in d for d in desc_texts)


def test_normalized_image_records_from_cdp_scraper_dicts():
    """Real CdpBridgeScraper._process_images-shaped dicts normalize with provenance."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-cdp-001",
        image_descriptions=[],
        image_ocr_texts=[],
        image_records=_make_cdp_image_records(),
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert len(result.images) == 2

    # First image: GPT-5.4 success path
    img0 = result.images[0]
    assert img0.file_path == "images/post_001/000.jpg"  # path → file_path
    assert img0.path == "images/post_001/000.jpg"
    assert img0.llm_description == "图片显示涂胶显影设备市场格局，TEL占95%份额"
    assert img0.ocr_text == "涂胶显影设备 TEL 95% 市场份额"
    assert img0.provider == "gpt5"  # vision_provider → provider
    assert img0.vision_model == "gpt-5.4"
    assert img0.fallback_chain == ["gpt5:ok"]
    assert img0.error == ""

    # Second image: SiliconFlow fallback path
    img1 = result.images[1]
    assert img1.file_path == "images/post_001/001.png"
    assert img1.path == "images/post_001/001.png"
    assert img1.provider == "siliconflow"
    assert img1.vision_model == "Qwen3-VL-32B-Instruct"
    assert img1.fallback_chain == ["gpt5:error:timeout", "siliconflow:ok"]

    # Provider summary includes vision providers
    assert "gpt5" in result.provider_summary or "siliconflow" in result.provider_summary


def test_cdp_image_records_to_dict_preserves_provenance():
    """to_dict() preserves path, llm_desc, vision_provider, vision_model, fallback_chain, error."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-cdp-002",
        image_descriptions=[],
        image_ocr_texts=[],
        image_records=_make_cdp_image_records(),
    )
    result = service.extract(request)

    d = result.to_dict()
    img0 = d["images"][0]
    assert img0["path"] == "images/post_001/000.jpg"
    assert img0["llm_description"] == "图片显示涂胶显影设备市场格局，TEL占95%份额"
    assert img0["provider"] == "gpt5"
    assert img0["vision_model"] == "gpt-5.4"
    assert img0["fallback_chain"] == ["gpt5:ok"]
    assert img0["error"] == ""

    img1 = d["images"][1]
    assert img1["path"] == "images/post_001/001.png"
    assert img1["provider"] == "siliconflow"
    assert img1["vision_model"] == "Qwen3-VL-32B-Instruct"
    assert img1["fallback_chain"] == ["gpt5:error:timeout", "siliconflow:ok"]


def test_cdp_image_record_with_source_url():
    """Image record with source_url preserves it through normalization."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-cdp-003",
        image_descriptions=[],
        image_ocr_texts=[],
        image_records=[
            {
                "filename": "000.jpg",
                "path": "images/post_x/000.jpg",
                "llm_desc": "test desc",
                "vision_provider": "gpt5",
                "vision_model": "gpt-5.4",
                "fallback_chain": ["gpt5:ok"],
                "error": "",
                "source_url": "https://images.zsxq.com/abc123",
            }
        ],
    )
    result = service.extract(request)
    img = result.images[0]
    assert img.source_url == "https://images.zsxq.com/abc123"
    assert result.to_dict()["images"][0]["source_url"] == "https://images.zsxq.com/abc123"


# ---------------------------------------------------------------------------
# Step 3: Fake LLM extracts VisionFact and renders to_prompt_context()
# ---------------------------------------------------------------------------


def test_fake_llm_extracts_visual_facts():
    """With a fake LLM backend, extract structured visual facts."""
    backend = _FakeLLMBackend()
    service = VisionEvidenceService(llm_backend=backend)
    request = VisionEvidenceRequest(
        article_id="test-006",
        image_descriptions=["涂胶显影设备 TEL 占 95%"],
        image_ocr_texts=["TEL 涂胶显影 95%"],
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert len(result.visual_facts) >= 1
    fact = result.visual_facts[0]
    assert isinstance(fact, VisionFact)
    assert "TEL" in fact.fact
    assert fact.confidence > 0


def test_fake_llm_renders_prompt_context():
    """to_prompt_context() returns markdown suitable for LLM thesis extraction."""
    backend = _FakeLLMBackend()
    service = VisionEvidenceService(llm_backend=backend)
    request = VisionEvidenceRequest(
        article_id="test-007",
        image_descriptions=["涂胶显影 TEL 95%", "刻蚀 TEL 30%"],
        image_ocr_texts=["涂胶显影 95%", "刻蚀 30%"],
    )
    result = service.extract(request)

    ctx = result.to_prompt_context()
    assert isinstance(ctx, str)
    assert "图片结构化事实" in ctx
    assert "TEL" in ctx


def test_to_prompt_context_empty_when_no_facts():
    """to_prompt_context() returns empty string when no visual facts."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-008",
        image_descriptions=[],
        image_ocr_texts=[],
    )
    result = service.extract(request)

    assert result.to_prompt_context() == ""


def test_no_llm_backend_marks_data_gap():
    """When no LLM backend is available, mark visual_fact_llm_unavailable."""
    service = VisionEvidenceService(llm_backend=None)
    request = VisionEvidenceRequest(
        article_id="test-009",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)

    assert result.status == "ok"  # images exist, just facts unavailable
    # No LLM → facts list empty, data_gap indicates why
    assert result.visual_facts == []
    assert any("llm_unavailable" in gap or "visual_fact" in gap for gap in result.data_gaps)


def test_llm_extraction_failure_graceful():
    """When LLM returns junk, visual facts stay empty with warning."""
    backend = _FailingLLMBackend()
    service = VisionEvidenceService(llm_backend=backend)
    request = VisionEvidenceRequest(
        article_id="test-010",
        image_descriptions=["some image data"],
        image_ocr_texts=["some ocr data"],
    )
    result = service.extract(request)

    # Should not crash — images are still normalized, facts just empty
    assert result.status == "ok"
    assert result.visual_facts == []
    assert any(
        "json" in w.lower() or "llm" in w.lower() or "visual" in w.lower()
        for w in result.warnings
    )


# ---------------------------------------------------------------------------
# Step 4: to_dict() copy isolation
# ---------------------------------------------------------------------------


def test_to_dict_round_trip():
    """to_dict() produces a plain dict with all key fields."""
    backend = _FakeLLMBackend()
    service = VisionEvidenceService(llm_backend=backend)
    request = VisionEvidenceRequest(
        article_id="test-011",
        image_descriptions=["测试描述"],
        image_ocr_texts=["测试OCR"],
    )
    result = service.extract(request)
    d = result.to_dict()

    assert isinstance(d, dict)
    assert d["article_id"] == "test-011"
    assert d["status"] == "ok"
    assert "images" in d
    assert "visual_facts" in d
    assert "data_gaps" in d
    assert "warnings" in d
    assert "provider_summary" in d
    assert d["source_boundary"] == "sensory"
    assert d["advisory_only"] is True
    assert d["trading_decision"] is False
    assert d["execution_allowed"] is False


def test_to_dict_is_copy_isolated():
    """Modifying the returned dict does not affect the original result."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-012",
        image_descriptions=["test"],
        image_ocr_texts=["test"],
    )
    result = service.extract(request)

    d1 = result.to_dict()
    d2 = result.to_dict()
    d1["article_id"] = "mutated"
    d1["status"] = "mutated"
    d1["images"] = []

    # Second copy is unaffected
    assert d2["article_id"] == "test-012"
    assert d2["status"] == "ok"

    # Original result is unaffected
    assert result.article_id == "test-012"


def test_to_dict_company_recommendations_deep_copied():
    """Modifying inner dicts of company_recommendations does not affect original."""
    result = VisionEvidenceResult(
        article_id="test-deep",
        status="ok",
        images=[],
        visual_facts=[],
        company_recommendations=[
            {"company": "TEL", "score": 95},
            {"company": "芯源微", "score": 80},
        ],
        prompt_context="",
        data_gaps=(),
        warnings=(),
        provider_summary="",
        source_boundary="sensory",
        advisory_only=True,
    )

    d = result.to_dict()
    # Mutate inner dict
    d["company_recommendations"][0]["company"] = "MUTATED"
    d["company_recommendations"][0]["score"] = 999

    # Original is unaffected
    assert result.company_recommendations[0]["company"] == "TEL"
    assert result.company_recommendations[0]["score"] == 95


def test_vision_evidence_service_default_auto_resolves_llm():
    """VisionEvidenceService() without args attempts to auto-resolve CognitionLLM.

    When config is unavailable (no API keys in test env), the service still
    constructs without error — the auto-resolve is best-effort.
    """
    # Default constructor should not raise
    service = VisionEvidenceService()
    assert service is not None
    # The internal backend may be None if config unavailable, but the service
    # must still be usable for image normalization
    request = VisionEvidenceRequest(
        article_id="test-default-llm",
        image_descriptions=["test description"],
        image_ocr_texts=["test ocr"],
    )
    result = service.extract(request)
    assert result.status == "ok"
    assert len(result.images) >= 1


def test_explicit_none_llm_backend_no_fact_extraction():
    """Explicit llm_backend=None means NO LLM — visual_fact_llm_unavailable."""
    service = VisionEvidenceService(llm_backend=None)
    request = VisionEvidenceRequest(
        article_id="test-explicit-none",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)
    assert result.visual_facts == []
    assert any(
        "llm_unavailable" in gap or "visual_fact" in gap for gap in result.data_gaps
    )


def test_vision_evidence_result_repr():
    """VisionEvidenceResult has a usable repr."""
    service = VisionEvidenceService()
    request = VisionEvidenceRequest(
        article_id="test-013",
        image_descriptions=[],
        image_ocr_texts=[],
    )
    result = service.extract(request)
    r = repr(result)
    assert "VisionEvidenceResult" in r
    assert "test-013" in r


# ---------------------------------------------------------------------------
# Step 5: VisionFact DTO
# ---------------------------------------------------------------------------


def test_vision_fact_dto():
    """VisionFact holds a structured fact with companies and metrics."""
    fact = VisionFact(
        fact="钼前驱体最具性价比",
        companies=["某公司A"],
        metrics={"score": "14.5"},
        confidence=0.85,
        image_ref="img_1",
    )
    assert fact.fact == "钼前驱体最具性价比"
    assert fact.companies == ["某公司A"]
    assert fact.metrics == {"score": "14.5"}
    assert fact.confidence == 0.85
    assert fact.image_ref == "img_1"

    d = fact.to_dict()
    assert d["fact"] == "钼前驱体最具性价比"
    assert d["companies"] == ["某公司A"]


# ---------------------------------------------------------------------------
# Step 6: Empty visual facts → prompt_context is empty
# ---------------------------------------------------------------------------


def test_prompt_context_empty_for_no_facts():
    """to_prompt_context returns '' when visual_facts is empty."""
    result = VisionEvidenceResult(
        article_id="test",
        status="ok",
        images=[],
        visual_facts=[],
        company_recommendations=[],
        prompt_context="",
        data_gaps=(),
        warnings=(),
        provider_summary="",
        source_boundary="sensory",
        advisory_only=True,
    )
    assert result.to_prompt_context() == ""


# ---------------------------------------------------------------------------
# Step 7: LLM fact-extraction failures produce STABLE, machine-readable gaps
#
# The advisory/source boundary and normalized image evidence must be
# preserved, but a visual-fact-extraction failure must never look like a
# successful (fact-complete) result. Each failure mode records a distinct,
# stable data_gap so downstream cognition can detect the missing facts.
# ---------------------------------------------------------------------------


def test_llm_call_exception_records_stable_data_gap():
    """complete() raising → stable visual_fact_llm_failed gap, boundary intact."""
    service = VisionEvidenceService(llm_backend=_RaisingLLMBackend())
    request = VisionEvidenceRequest(
        article_id="gap-exc",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert result.visual_facts == []
    assert "visual_fact_llm_failed" in result.data_gaps
    assert len(result.images) >= 1
    assert result.source_boundary == "sensory"
    assert result.advisory_only is True


def test_llm_empty_response_records_stable_data_gap():
    """Empty LLM response → stable visual_fact_llm_empty gap, boundary intact."""
    service = VisionEvidenceService(llm_backend=_EmptyLLMBackend())
    request = VisionEvidenceRequest(
        article_id="gap-empty",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert result.visual_facts == []
    assert "visual_fact_llm_empty" in result.data_gaps
    assert len(result.images) >= 1
    assert result.source_boundary == "sensory"
    assert result.advisory_only is True


def test_llm_parse_failure_records_stable_data_gap():
    """Unparseable JSON → stable visual_fact_llm_parse_failed gap, boundary intact."""
    service = VisionEvidenceService(llm_backend=_FailingLLMBackend())
    request = VisionEvidenceRequest(
        article_id="gap-parse",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert result.visual_facts == []
    assert "visual_fact_llm_parse_failed" in result.data_gaps
    assert len(result.images) >= 1
    assert result.source_boundary == "sensory"
    assert result.advisory_only is True


def test_llm_invalid_response_records_stable_data_gap():
    """Non-dict / malformed facts → stable visual_fact_llm_invalid_response gap."""
    service = VisionEvidenceService(llm_backend=_NonDictLLMBackend())
    request = VisionEvidenceRequest(
        article_id="gap-invalid",
        image_descriptions=["刻蚀设备 TEL 占 30% 份额"],
        image_ocr_texts=["TEL 30%"],
    )
    result = service.extract(request)

    assert result.status == "ok"
    assert result.visual_facts == []
    assert "visual_fact_llm_invalid_response" in result.data_gaps
    assert len(result.images) >= 1
    assert result.source_boundary == "sensory"
    assert result.advisory_only is True
