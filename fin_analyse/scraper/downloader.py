"""图片下载和 OCR"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .image_downloader import download_and_ocr_images as do_download

logger = logging.getLogger(__name__)


def _provider_name(tag: str) -> str:
    return tag.split(":", 1)[0]


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content.strip() if isinstance(content, str) else ""


class ImageDownloader:
    """协调图片下载和 OCR，管理图片目录结构"""

    def __init__(self, cookies_provider):
        """
        cookies_provider: 可调用对象，返回 dict[str, str] 的 cookies
        """
        self._get_cookies = cookies_provider

    def download_and_ocr(
        self, images: list[dict], post_id: str = "", max_images: int = 5
    ) -> list[dict]:
        """用浏览器 Cookie + requests 下载图片并 OCR，保存到 images/{post_id}/"""
        post_img_dir = config.IMAGES_DIR / post_id if post_id else config.IMAGES_DIR
        cookies = self._get_cookies()
        urls = [img["src"] for img in images[:max_images]]
        results = do_download(urls, cookies, str(post_img_dir))

        # Fix paths to be relative to KB_ROOT
        for r in results:
            r["path"] = str(Path(r["path"]).relative_to(config.KB_ROOT)) if post_id else r["path"]
        return results


_DEFAULT_VISION_CHAIN = ("mimo", "glm-vision", "vision")

# chain 条目缺省 model 名（条目未写 model 时兜底，与配置化前行为一致）
_DEFAULT_VISION_MODELS = {
    "mimo": "mimo-v2.5",
    "glm-vision": "glm-4.6v-flash",
    "vision": "Qwen/Qwen3-VL-30B-A3B-Instruct",
}


def _vision_chain(cfg: dict) -> list[str]:
    """识图 fallback 链：读 llm.yaml vision.chain，保序去重。

    vision 段/chain 键缺失或非 list → 现状缺省序（配置缺失 = 行为不变）；
    空链或条目全非法是合法显式配置（返回空）。
    """
    vision_cfg = cfg.get("vision")
    if not isinstance(vision_cfg, dict):
        return list(_DEFAULT_VISION_CHAIN)
    chain = vision_cfg.get("chain")
    if chain is None or not isinstance(chain, list):
        return list(_DEFAULT_VISION_CHAIN)
    names: list[str] = []
    for item in chain:
        if isinstance(item, str) and item and item not in names:
            names.append(item)
    return names


def _get_vision_clients() -> list[tuple]:
    """获取 vision 模型 client 列表，按 llm.yaml vision.chain 配置顺序排列。

    条目 enabled/key 未解析 ${}/熔断打开则跳过；熔断键带 ``vision:`` 前缀，
    与同后端的文本链调用互不干扰。无可用模型返回 []。
    返回 [(client, model_name, tag, timeout, max_tokens), ...]
    """
    try:
        import openai
    except ImportError:
        return []

    clients: list[tuple] = []
    try:
        from fin_analyse.claims.backend_health import get_backend_circuit_breaker

        breaker = get_backend_circuit_breaker()
    except Exception:
        breaker = None

    try:
        from fin_analyse.claims.config_loader import load_llm_config

        cfg = load_llm_config()
        models = cfg.get("models") or {}
        for name in _vision_chain(cfg):
            entry = models.get(name)
            if not isinstance(entry, dict):
                continue
            breaker_key = f"vision:{name}"
            if (
                not entry.get("enabled")
                or not entry.get("api_key")
                or "${" in str(entry.get("api_key"))
                or "${" in str(entry.get("base_url", ""))
                or (breaker is not None and not breaker.can_try(breaker_key))
            ):
                continue
            try:
                model = entry.get("model") or _DEFAULT_VISION_MODELS.get(name, name)
                clients.append(
                    (
                        openai.OpenAI(api_key=entry["api_key"], base_url=entry.get("base_url")),
                        model,
                        name,
                        int(entry.get("timeout", 30)),
                        int(entry.get("max_tokens", 1536)),
                    )
                )
            except Exception:
                if breaker:
                    breaker.record_failure(breaker_key, "vision_init_exhausted")
    except Exception:
        pass

    return clients


@dataclass
class ImageProvenance:
    """Structured provenance for a single image analysis result."""

    llm_desc: str = ""
    ocr_text: str = ""
    vision_provider: str = ""  # "mimo" | "glm-vision" | "vision" | "none"
    vision_model: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "llm_desc": self.llm_desc,
            "ocr_text": self.ocr_text,
            "vision_provider": self.vision_provider,
            "vision_model": self.vision_model,
            "fallback_chain": list(self.fallback_chain),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageProvenance":
        return cls(
            llm_desc=str(data.get("llm_desc", "")),
            ocr_text=str(data.get("ocr_text", "")),
            vision_provider=str(data.get("vision_provider", "")),
            vision_model=str(data.get("vision_model", "")),
            fallback_chain=list(data.get("fallback_chain", [])),
            error=str(data.get("error", "")),
        )


def describe_image_with_provenance(
    image_path: str, *, prompt: str | None = None
) -> ImageProvenance:
    """Analyze image with full fallback provenance tracking.

    ``prompt`` 缺省时沿用通用描述提示；调用方（定向识图/回填）可传入专用
    提示词，要求按固定格式转录评分表。

    Returns ImageProvenance recording which provider succeeded
    or which fallback path was used.

    Fallback chain: 按 llm.yaml vision.chain（缺省 mimo → GLM-4.6V-Flash →
    SiliconFlow Vision）→ OCR (final)
    """
    import base64 as b64

    img_path = Path(image_path)
    if not img_path.exists():
        return ImageProvenance(
            vision_provider="none",
            fallback_chain=["file_not_found"],
            error=f"File not found: {image_path}",
        )

    try:
        with open(img_path, "rb") as f:
            image_data = b64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return ImageProvenance(
            vision_provider="none",
            fallback_chain=["read_error"],
            error=str(e),
        )

    ext = img_path.suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

    if prompt is None:
        prompt = (
            "描述这张图片的内容。如果是K线图/走势图，描述趋势和关键形态；"
            "如果是表格，提取关键数据；如果是文字截图，转录全文。用中文回答。"
        )

    fallback_chain: list[str] = []
    clients = _get_vision_clients()
    remaining: dict[str, int] = {}
    for _client, _model, tag, _timeout, _max_tokens in clients:
        remaining[_provider_name(tag)] = remaining.get(_provider_name(tag), 0) + 1
    breaker = None
    try:
        from fin_analyse.claims.backend_health import get_backend_circuit_breaker

        breaker = get_backend_circuit_breaker()
    except Exception:
        pass

    for client, model, tag, timeout, max_tokens in clients:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_data}"},
                            },
                        ],
                    }
                ],
                max_tokens=max_tokens,
                temperature=0.3,
                timeout=timeout,
            )
            content = _response_text(response)
            if content.strip():
                if breaker:
                    breaker.record_success(f"vision:{_provider_name(tag)}")
                fallback_chain.append(f"{tag}:ok")
                logger.info("[IMG] %s (%s) → %d chars", img_path.name, tag, len(content))
                return ImageProvenance(
                    llm_desc=content,
                    vision_provider=_provider_name(tag),
                    vision_model=model,
                    fallback_chain=fallback_chain,
                )
            fallback_chain.append(f"{tag}:empty_response")
            provider = _provider_name(tag)
            remaining[provider] -= 1
            if remaining[provider] == 0 and breaker:
                breaker.record_failure(f"vision:{provider}", "vision_endpoints_exhausted")
            logger.debug("[IMG] %s (%s): empty response", img_path.name, tag)
        except Exception as e:
            err_short = str(e)[:80]
            fallback_chain.append(f"{tag}:error:{err_short}")
            provider = _provider_name(tag)
            remaining[provider] -= 1
            if remaining[provider] == 0 and breaker:
                breaker.record_failure(f"vision:{provider}", "vision_endpoints_exhausted")
            logger.warning("[IMG] %s (%s): %s", img_path.name, tag, err_short)
            continue

    if not clients:
        fallback_chain.append("no_vision_clients_available")

    logger.warning("[IMG] %s: all vision models failed, fallback to OCR", img_path.name)
    return ImageProvenance(
        llm_desc="",
        vision_provider="none",
        vision_model="",
        fallback_chain=fallback_chain,
        error="All vision models failed" if clients else "No vision clients configured",
    )


def describe_image(image_path: str) -> str:
    """用 LLM vision 模型描述图片内容。失败返回空字符串（OCR 兜底）。

    按 llm.yaml vision.chain 顺序尝试（owner 2026-08-28 重排：glm53_flash →
    GLM-4.6V-Flash → SiliconFlow → mimo）。全部失败时降级到纯 OCR。
    """
    import base64
    from pathlib import Path

    candidates = _get_vision_clients()
    if not candidates:
        logger.debug("[LLM] 无可用 vision 模型，跳过图片描述")
        return ""

    remaining: dict[str, int] = {}
    for _client, _model, tag, _timeout, _max_tokens in candidates:
        provider = _provider_name(tag)
        remaining[provider] = remaining.get(provider, 0) + 1
    breaker = None
    try:
        from fin_analyse.claims.backend_health import get_backend_circuit_breaker

        breaker = get_backend_circuit_breaker()
    except Exception:
        pass

    img_path = Path(image_path)
    if not img_path.exists():
        return ""

    try:
        with open(img_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return ""

    ext = img_path.suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

    prompt = (
        "描述这张图片的内容。如果是K线图/走势图，描述趋势和关键形态；"
        "如果是表格，提取关键数据；如果是文字截图，转录全文。用中文回答。"
    )

    for trial_client, trial_model, tag, timeout, max_tokens in candidates:
        try:
            response = trial_client.chat.completions.create(
                model=trial_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_data}"},
                            },
                        ],
                    }
                ],
                max_tokens=max_tokens,
                temperature=0.3,
                timeout=timeout,
            )
            content = _response_text(response)
            if content:
                if breaker:
                    breaker.record_success(f"vision:{_provider_name(tag)}")
                logger.info("[LLM] 图片描述 (%s): %s → %d chars", tag, img_path.name, len(content))
                return content
            logger.debug("[LLM] 图片描述返回空 (%s): %s", tag, img_path.name)
            provider = _provider_name(tag)
            remaining[provider] -= 1
            if remaining[provider] == 0 and breaker:
                breaker.record_failure(f"vision:{provider}", "vision_endpoints_exhausted")
        except Exception as e:
            logger.warning("[LLM] 图片描述失败 (%s): %s — %s", tag, img_path.name, e)
            provider = _provider_name(tag)
            remaining[provider] -= 1
            if remaining[provider] == 0 and breaker:
                breaker.record_failure(f"vision:{provider}", "vision_endpoints_exhausted")
            continue

    logger.warning("[LLM] 所有 vision 模型均失败: %s (将使用 OCR 兜底)", img_path.name)
    return ""
