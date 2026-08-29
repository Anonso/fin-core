"""Download images using browser cookies to bypass auth."""

import io
import logging
import time

import requests
from PIL import Image
from pytesseract import image_to_string

logger = logging.getLogger(__name__)


def download_and_ocr_images(
    image_urls: list[str], cookies: dict[str, str], output_dir: str
) -> list[dict]:
    """Download images and OCR, return list of {filename, path, ocr_text}."""
    from pathlib import Path

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    results = []

    session = requests.Session()
    for cookie_name, cookie_value in cookies.items():
        session.cookies.set(cookie_name, cookie_value)

    for i, url in enumerate(image_urls):
        try:
            resp = session.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    "Referer": "https://wx.zsxq.com/",
                },
            )
            if resp.status_code != 200:
                continue

            img_bytes = resp.content
            ext = "jpg" if resp.headers.get("Content-Type", "").startswith("image/jpeg") else "png"
            filename = f"{i:03d}.{ext}"
            filepath = out / filename
            filepath.write_bytes(img_bytes)

            ocr_text = ""
            try:
                img = Image.open(io.BytesIO(img_bytes))
                raw = image_to_string(img, lang="chi_sim+eng", config="--psm 6").strip()
                # 仅保留有实际内容的OCR: 中文字符≥10且总长度≥50
                chinese_chars = sum(1 for c in raw if "一" <= c <= "鿿")
                if chinese_chars >= 10 and len(raw) >= 50:
                    ocr_text = raw
            except Exception:
                pass

            results.append(
                {
                    "filename": filename,
                    "path": str(filepath),
                    "ocr_text": ocr_text,
                }
            )

            if ocr_text:
                logger.info("[OCR] %s: %d chars", filename, len(ocr_text))
            else:
                logger.debug("[IMG] %s: saved (no useful text)", filename)

            time.sleep(0.5)  # rate limit
        except Exception as e:
            logger.warning("[WARN] image %d: %s", i, e)

    return results
