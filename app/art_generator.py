from __future__ import annotations

from html import unescape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
import mimetypes
import re
import uuid

import requests
from PIL import Image, ImageDraw, ImageFont
from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .models import Post
from .storage import key_from_media_url, local_path_from_url, open_media_bytes, save_bytes

META_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?P<key>[^"\']+)["\'][^>]+content=["\'](?P<value>[^"\']*)["\'][^>]*>',
    re.IGNORECASE,
)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")

VARIANT_SPECS = {
    "feed": {
        "label": "Feed",
        "width": 1080,
        "height": 1440,
        "max_size": 44,
        "min_size": 26,
        "max_lines": 4,
        "bottom_gap": 58,
        "text_top_padding": 28,
        "badge_gap": 18,
        "badge_padding_x": 24,
        "badge_padding_y": 12,
    },
    "stories": {
        "label": "Stories",
        "width": 1080,
        "height": 1920,
        "max_size": 54,
        "min_size": 30,
        "max_lines": 4,
        "bottom_gap": 78,
        "text_top_padding": 30,
        "badge_gap": 20,
        "badge_padding_x": 24,
        "badge_padding_y": 12,
    },
    # Assumido como 1080x1080 para Facebook. O pedido veio como 1080x0180, provavelmente um typo.
    "facebook": {
        "label": "Facebook",
        "width": 1080,
        "height": 1080,
        "max_size": 38,
        "min_size": 24,
        "max_lines": 4,
        "bottom_gap": 50,
        "text_top_padding": 24,
        "badge_gap": 16,
        "badge_padding_x": 22,
        "badge_padding_y": 10,
    },
}


class ArtGeneratorError(Exception):
    pass


def _asset_path(filename: str) -> Path:
    return Path(current_app.root_path) / "static" / "gerador" / filename


def _absolute_to_media_local(url: str) -> Path | None:
    return local_path_from_url(url)


def _guess_extension(filename: str, content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if ext == ".jpeg" else ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    return ".jpg"


def _save_bytes(content: bytes, folder: str, filename_hint: str = "file") -> str:
    return save_bytes(content, folder=folder, filename_hint=filename_hint)


def save_uploaded_image(upload: FileStorage | None, folder: str = "gerador/source") -> str:
    if not upload or not upload.filename:
        return ""
    filename = secure_filename(upload.filename)
    content = upload.read()
    upload.stream.seek(0)
    if not content:
        return ""
    return _save_bytes(content, folder=folder, filename_hint=filename)


def _normalize_text(value: str | None) -> str:
    text = unescape((value or "").strip())
    return WHITESPACE_RE.sub(" ", text).strip()


def _extract_slug(article_url: str) -> str:
    path = urlparse(article_url).path or ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    slug = parts[-1]
    if slug.lower() in {"p", "c", "buscar"} and len(parts) >= 2:
        slug = parts[-2]
    return slug.strip()


def _featured_img_from_embed(post_payload: dict[str, Any]) -> str:
    try:
        media = post_payload.get("_embedded", {}).get("wp:featuredmedia", [])
        if media and media[0].get("source_url"):
            return media[0]["source_url"]
    except Exception:
        pass
    return ""


def _parse_meta_tags(html: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for match in META_TAG_RE.finditer(html or ""):
        data[match.group("key").strip().lower()] = _normalize_text(match.group("value"))
    title_match = TITLE_TAG_RE.search(html or "")
    if title_match:
        data.setdefault("title", _normalize_text(title_match.group(1)))
    return data


def resolve_post_source(article_url: str) -> dict[str, str]:
    url = (article_url or "").strip()
    if not url:
        return {}

    slug = _extract_slug(url)
    resolved = {"title": "", "image": "", "category": "", "slug": slug}

    if slug:
        post = Post.query.filter_by(slug=slug).first()
        if post:
            resolved.update(
                {
                    "title": _normalize_text(post.title),
                    "image": (post.featured_image or "").strip(),
                    "category": _normalize_text(post.categories[0].name) if post.categories else "",
                }
            )

    wp_base = (current_app.config.get("WP_BASE_URL") or "").rstrip("/")
    if slug and (not resolved["title"] or not resolved["image"] or not resolved["category"]) and wp_base:
        try:
            api_url = urljoin(wp_base + "/", "wp-json/wp/v2/posts")
            response = requests.get(
                api_url,
                params={"slug": slug, "_embed": 1, "status": "publish", "per_page": 1},
                timeout=15,
            )
            response.raise_for_status()
            posts = response.json() or []
            if posts:
                post_payload = posts[0]
                title = _normalize_text(((post_payload.get("title") or {}).get("rendered") or ""))
                image = _featured_img_from_embed(post_payload)
                category_name = ""
                for cat_id in (post_payload.get("categories") or []):
                    try:
                        cat_response = requests.get(
                            urljoin(wp_base + "/", f"wp-json/wp/v2/categories/{cat_id}"),
                            timeout=10,
                        )
                        if cat_response.ok:
                            category_name = _normalize_text((cat_response.json() or {}).get("name") or "")
                            if category_name:
                                break
                    except Exception:
                        continue
                if title and not resolved["title"]:
                    resolved["title"] = title
                if image and not resolved["image"]:
                    resolved["image"] = image
                if category_name and not resolved["category"]:
                    resolved["category"] = category_name
        except Exception:
            pass

    if not resolved["title"] or not resolved["image"] or not resolved["category"]:
        try:
            response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            meta = _parse_meta_tags(response.text)
            if not resolved["title"]:
                resolved["title"] = meta.get("og:title") or meta.get("twitter:title") or meta.get("title") or ""
            if not resolved["image"]:
                resolved["image"] = meta.get("og:image") or meta.get("twitter:image") or ""
            if not resolved["category"]:
                resolved["category"] = meta.get("article:section") or meta.get("og:section") or ""
        except Exception:
            pass

    return resolved


def _fetch_image(source: str) -> Image.Image | None:
    url = (source or "").strip()
    if not url:
        return None

    local_path = _absolute_to_media_local(url)
    if local_path and local_path.exists():
        with Image.open(local_path) as image:
            return image.convert("RGBA")

    if url.startswith(current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/") + "/"):
        content, _content_type, _name = open_media_bytes(url)
        with Image.open(BytesIO(content)) as image:
            return image.convert("RGBA")

    if url.startswith("/") and Path(url).exists():
        with Image.open(url) as image:
            return image.convert("RGBA")

    response = requests.get(url, timeout=25)
    response.raise_for_status()
    with Image.open(BytesIO(response.content)) as image:
        return image.convert("RGBA")


def _cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    dst_w, dst_h = size
    src_w, src_h = image.size
    scale = max(dst_w / src_w, dst_h / src_h)
    new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
    resized = image.resize(new_size, Image.Resampling.LANCZOS)
    x = (resized.width - dst_w) // 2
    y = (resized.height - dst_h) // 2
    return resized.crop((x, y, x + dst_w, y + dst_h))


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_asset_path("Montserrat-ExtraBold.ttf")), size=size)


def _text_bbox(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int, int, int]:
    if not text:
        return 0, 0, 0, 0
    left, top, right, bottom = font.getbbox(text)
    return int(left), int(top), int(right), int(bottom)


def _wrap_text(text: str, max_size: int, max_width: int, max_lines: int, min_size: int = 10) -> tuple[list[str], int]:
    words = [word for word in text.split() if word]
    if not words:
        return [], max_size

    size = max_size
    while size >= min_size:
        font = _load_font(size)
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            left, _top, right, _bottom = _text_bbox(font, trial)
            width = right - left
            if width <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= max_lines:
            return lines, size
        size -= 2
    return words[:max_lines], max(min_size, size)


def _fit_single_line_text(text: str, max_size: int, max_width: int, min_size: int = 14) -> tuple[ImageFont.FreeTypeFont, int]:
    size = max_size
    while size >= min_size:
        font = _load_font(size)
        left, _top, right, _bottom = _text_bbox(font, text)
        if (right - left) <= max_width:
            return font, size
        size -= 1
    return _load_font(min_size), min_size


def _line_metrics(font: ImageFont.FreeTypeFont, line: str) -> tuple[int, int, int, int, int, int]:
    left, top, right, bottom = _text_bbox(font, line)
    return left, top, right, bottom, right - left, bottom - top


def _build_text_layout(
    *,
    spec: dict[str, int | str],
    width: int,
    height: int,
    fade_height: int,
    title_text: str,
    category: str,
    pad_left: int,
    pad_right: int,
) -> dict[str, object]:
    text_region_top = max(0, height - fade_height + int(spec.get("text_top_padding", 24)))
    text_region_bottom = height - int(spec.get("bottom_gap", 48))
    available_height = max(1, text_region_bottom - text_region_top)
    max_width = width - (pad_left + pad_right)
    max_lines = int(spec.get("max_lines", 4))
    max_size = int(spec.get("max_size", 40))
    min_size = int(spec.get("min_size", 20))

    chosen: dict[str, object] | None = None
    for size in range(max_size, min_size - 1, -2):
        lines, used_size = _wrap_text(
            title_text,
            max_size=size,
            max_width=max_width,
            max_lines=max_lines,
            min_size=min_size,
        )
        title_font = _load_font(used_size)
        line_gap = max(8, int(round(used_size * 0.22)))
        line_boxes = [_line_metrics(title_font, line) for line in lines]
        title_height = sum(box[5] for box in line_boxes)
        if len(line_boxes) > 1:
            title_height += line_gap * (len(line_boxes) - 1)

        category_payload: dict[str, object] | None = None
        category_height = 0
        badge_gap = 0
        if category:
            category_font_max = max(18, int(round(used_size * 0.5)))
            category_font, category_size = _fit_single_line_text(
                category,
                max_size=category_font_max,
                max_width=max_width - (int(spec.get("badge_padding_x", 24)) * 2),
                min_size=14,
            )
            cat_left, cat_top, cat_right, cat_bottom = _text_bbox(category_font, category)
            cat_width = cat_right - cat_left
            cat_height = cat_bottom - cat_top
            badge_pad_x = int(spec.get("badge_padding_x", 24))
            badge_pad_y = int(spec.get("badge_padding_y", 12))
            category_height = cat_height + badge_pad_y * 2
            badge_gap = int(spec.get("badge_gap", 18))
            category_payload = {
                "font": category_font,
                "size": category_size,
                "bbox": (cat_left, cat_top, cat_right, cat_bottom),
                "text_width": cat_width,
                "text_height": cat_height,
                "pad_x": badge_pad_x,
                "pad_y": badge_pad_y,
                "height": category_height,
            }

        block_height = title_height + category_height + (badge_gap if category and title_height else 0)
        if block_height <= available_height:
            chosen = {
                "lines": lines,
                "title_font": title_font,
                "line_boxes": line_boxes,
                "line_gap": line_gap,
                "title_height": title_height,
                "category": category_payload,
                "block_height": block_height,
                "text_region_top": text_region_top,
                "text_region_bottom": text_region_bottom,
                "badge_gap": badge_gap,
            }
            break

    if chosen is None:
        lines, used_size = _wrap_text(
            title_text,
            max_size=min_size,
            max_width=max_width,
            max_lines=max_lines,
            min_size=min_size,
        )
        title_font = _load_font(used_size)
        line_gap = max(8, int(round(used_size * 0.22)))
        line_boxes = [_line_metrics(title_font, line) for line in lines]
        title_height = sum(box[5] for box in line_boxes)
        if len(line_boxes) > 1:
            title_height += line_gap * (len(line_boxes) - 1)
        chosen = {
            "lines": lines,
            "title_font": title_font,
            "line_boxes": line_boxes,
            "line_gap": line_gap,
            "title_height": title_height,
            "category": None,
            "block_height": title_height,
            "text_region_top": text_region_top,
            "text_region_bottom": text_region_bottom,
            "badge_gap": int(spec.get("badge_gap", 18)),
        }

    return chosen


def _draw_rounded_rectangle(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: tuple[int, int, int, int]):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def generate_art_image(title: str, image_source: str, variant: str, include_title: bool = True, category_text: str = "") -> Image.Image:
    spec = VARIANT_SPECS[variant]
    width = spec["width"]
    height = spec["height"]

    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))

    if image_source:
        try:
            background = _fetch_image(image_source)
        except Exception as exc:
            raise ArtGeneratorError(f"Não foi possível carregar a imagem informada: {exc}") from exc
        if background is not None:
            canvas.alpha_composite(_cover_resize(background, (width, height)))

    fade_height = int(height * 0.38)
    fade_path = _asset_path("fadepop.png")
    if fade_path.exists():
        with Image.open(fade_path) as fade_img:
            fade = fade_img.convert("RGBA")
        scale = width / fade.width
        fade = fade.resize((width, max(1, int(fade.height * scale))), Image.Resampling.LANCZOS)
        fade_height = fade.height
        canvas.alpha_composite(fade, (0, height - fade.height))

    logo_path = _asset_path("paranapop.png")
    if logo_path.exists():
        with Image.open(logo_path) as logo_img:
            logo = logo_img.convert("RGBA")
        max_logo_width = int(width * 0.22)
        scale = min(max_logo_width / logo.width, 1)
        logo = logo.resize((max(1, int(logo.width * scale)), max(1, int(logo.height * scale))), Image.Resampling.LANCZOS)
        canvas.alpha_composite(logo, (64, 64))

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    yellow = (255, 218, 0, 255)
    black = (0, 0, 0, 255)

    title_text = _normalize_text(title).upper()
    category = _normalize_text(category_text).upper()
    pad_left = 64
    pad_right = 64

    if include_title and title_text:
        layout = _build_text_layout(
            spec=spec,
            width=width,
            height=height,
            fade_height=fade_height,
            title_text=title_text,
            category=category,
            pad_left=pad_left,
            pad_right=pad_right,
        )
        block_bottom = int(layout["text_region_bottom"])
        block_top = block_bottom - int(layout["block_height"])
        current_y = block_top

        category_payload = layout.get("category")
        if category_payload:
            cat_left, cat_top, _cat_right, _cat_bottom = category_payload["bbox"]
            pad_x = int(category_payload["pad_x"])
            pad_y = int(category_payload["pad_y"])
            cat_height = int(category_payload["height"])
            x1 = pad_left
            y1 = current_y
            x2 = x1 + int(category_payload["text_width"]) + pad_x * 2
            y2 = y1 + cat_height
            _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
            draw.text((x1 + pad_x - cat_left, y1 + pad_y - cat_top), category, font=category_payload["font"], fill=black)
            current_y = y2 + int(layout["badge_gap"])

        title_font = layout["title_font"]
        line_boxes = layout["line_boxes"]
        line_gap = int(layout["line_gap"])
        for index, line in enumerate(layout["lines"]):
            left, top, _right, _bottom, _line_width, line_height = line_boxes[index]
            draw.text((pad_left - left, current_y - top), line, font=title_font, fill=white)
            current_y += line_height + line_gap
    elif category:
        max_width = width - (pad_left + pad_right)
        tag_font, _size = _fit_single_line_text(category, max_size=22, max_width=max_width - 44, min_size=14)
        left, top, right, bottom = _text_bbox(tag_font, category)
        text_width = right - left
        text_height = bottom - top
        pad_x = 22
        pad_y = 10
        x1 = pad_left
        y2 = height - int(spec.get("bottom_gap", 48))
        y1 = y2 - text_height - pad_y * 2
        x2 = x1 + text_width + pad_x * 2
        _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
        draw.text((x1 + pad_x - left, y1 + pad_y - top), category, font=tag_font, fill=black)

    return canvas



def generate_trivox_art_image(title: str, image_source: str, variant: str) -> Image.Image:
    """Gera a arte manual do Portal Trivox sem depender do antigo plugin WordPress."""
    spec = VARIANT_SPECS[variant]
    width = int(spec["width"])
    height = int(spec["height"])
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))

    if image_source:
        try:
            background = _fetch_image(image_source)
        except Exception as exc:
            raise ArtGeneratorError(f"Não foi possível carregar a imagem informada: {exc}") from exc
        if background is not None:
            canvas.alpha_composite(_cover_resize(background, (width, height)))

    # Escurecimento inferior próprio do Trivox para preservar leitura do título.
    fade_height = int(height * 0.46)
    fade = Image.new("RGBA", (width, fade_height), (0, 0, 0, 0))
    fade_draw = ImageDraw.Draw(fade)
    for y in range(fade_height):
        ratio = y / max(1, fade_height - 1)
        alpha = int(225 * (ratio ** 1.7))
        fade_draw.line((0, y, width, y), fill=(0, 0, 0, alpha))
    canvas.alpha_composite(fade, (0, height - fade_height))

    logo_path = _asset_path("trivox.png")
    if logo_path.exists():
        with Image.open(logo_path) as logo_img:
            logo = logo_img.convert("RGBA")
        max_logo_width = int(width * 0.26)
        max_logo_height = int(height * 0.10)
        scale = min(max_logo_width / logo.width, max_logo_height / logo.height, 1)
        logo = logo.resize((max(1, int(logo.width * scale)), max(1, int(logo.height * scale))), Image.Resampling.LANCZOS)
        canvas.alpha_composite(logo, (64, 64))

    title_text = _normalize_text(title).upper()
    if title_text:
        draw = ImageDraw.Draw(canvas)
        pad_left = 64
        pad_right = 64
        max_width = width - pad_left - pad_right
        # Usa a mesma tipografia instalada no gerador atual, mas sem badge de categoria.
        lines, used_size = _wrap_text(
            title_text,
            max_size=int(spec.get("max_size", 44)) + 4,
            max_width=max_width,
            max_lines=int(spec.get("max_lines", 4)),
            min_size=int(spec.get("min_size", 24)),
        )
        font = _load_font(used_size)
        line_gap = max(8, int(round(used_size * 0.20)))
        boxes = [_line_metrics(font, line) for line in lines]
        total_height = sum(box[5] for box in boxes) + line_gap * max(0, len(boxes) - 1)
        current_y = height - int(spec.get("bottom_gap", 58)) - total_height
        # Filete na cor institucional para amarrar a identidade visual.
        accent_y = max(height - fade_height + 24, current_y - 26)
        draw.rounded_rectangle((pad_left, accent_y, pad_left + 118, accent_y + 10), radius=5, fill=(0, 62, 77, 255))
        for index, line in enumerate(lines):
            left, top, _right, _bottom, _line_width, line_height = boxes[index]
            draw.text((pad_left - left, current_y - top), line, font=font, fill=(255, 255, 255, 255))
            current_y += line_height + line_gap

    return canvas


def generate_trivox_variants(*, title: str, image_source: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for key, spec in VARIANT_SPECS.items():
        image = generate_trivox_art_image(title=title, image_source=image_source, variant=key)
        filename = f"trivox-arte-{key}-{uuid.uuid4().hex}.png"
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        url = save_bytes(buffer.getvalue(), folder="gerador/trivox-generated", filename_hint=filename, content_type="image/png")
        results.append({
            "key": key,
            "label": spec["label"],
            "size": f"{spec['width']}x{spec['height']}",
            "url": url,
            "download_key": key_from_media_url(url) or f"gerador/trivox-generated/{filename}",
            "download_name": filename,
        })
    return results

def build_generator_payload(
    *,
    post_url: str,
    custom_title: str,
    custom_category: str,
    custom_image_url: str,
    uploaded_file: FileStorage | None,
    include_title: bool,
    include_image: bool,
    use_post_title: bool,
    use_post_thumb: bool,
) -> dict[str, Any]:
    source_data = resolve_post_source(post_url) if post_url else {}

    title = ""
    if include_title:
        title = source_data.get("title", "") if use_post_title else ""
        if not title:
            title = _normalize_text(custom_title)

    image_source = ""
    if include_image:
        image_source = (source_data.get("image") or "").strip() if use_post_thumb else ""
        if not image_source and uploaded_file and uploaded_file.filename:
            image_source = save_uploaded_image(uploaded_file)
        if not image_source:
            image_source = (custom_image_url or "").strip()

    category = _normalize_text(custom_category) or _normalize_text(source_data.get("category"))

    return {
        "source": source_data,
        "title": title,
        "image_source": image_source,
        "category": category,
    }


def generate_variants(*, title: str, image_source: str, include_title: bool, category_text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    for key, spec in VARIANT_SPECS.items():
        image = generate_art_image(title=title, image_source=image_source, variant=key, include_title=include_title, category_text=category_text)
        filename = f"arte-{key}-{uuid.uuid4().hex}.png"
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        url = save_bytes(buffer.getvalue(), folder="gerador/generated", filename_hint=filename, content_type="image/png")
        results.append(
            {
                "key": key,
                "label": spec["label"],
                "size": f"{spec['width']}x{spec['height']}",
                "url": url,
                "download_key": key_from_media_url(url) or f"gerador/generated/{filename}",
                "download_name": filename,
            }
        )

    return results
