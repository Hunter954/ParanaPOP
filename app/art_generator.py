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

META_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?P<key>[^"\']+)["\'][^>]+content=["\'](?P<value>[^"\']*)["\'][^>]*>',
    re.IGNORECASE,
)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")

VARIANT_SPECS = {
    "feed": {"label": "Feed", "width": 1080, "height": 1440, "max_size": 44, "line_gap": 56, "bottom_gap": 46},
    "stories": {"label": "Stories", "width": 1080, "height": 1920, "max_size": 54, "line_gap": 66, "bottom_gap": 64},
    # Assumido como 1080x1080 para Facebook. O pedido veio como 1080x0180, provavelmente um typo.
    "facebook": {"label": "Facebook", "width": 1080, "height": 1080, "max_size": 38, "line_gap": 48, "bottom_gap": 44},
}


class ArtGeneratorError(Exception):
    pass


def _asset_path(filename: str) -> Path:
    return Path(current_app.root_path) / "static" / "gerador" / filename


def _media_relative_to_url(relative: str) -> str:
    prefix = current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")
    return f"{prefix}/{relative.lstrip('/')}"


def _absolute_to_media_local(url: str) -> Path | None:
    if not url:
        return None
    prefix = current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url
    if not path.startswith(prefix + "/"):
        return None
    relative = path[len(prefix) + 1 :]
    return Path(current_app.config["MEDIA_ROOT"]).resolve() / relative


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
    media_root = Path(current_app.config["MEDIA_ROOT"]).resolve()
    target_dir = media_root / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(filename_hint)
    filename = f"{uuid.uuid4().hex}{ext}"
    target_path = target_dir / filename
    target_path.write_bytes(content)
    return _media_relative_to_url((target_dir / filename).relative_to(media_root).as_posix())


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


def _wrap_text(text: str, max_size: int, max_width: int, max_lines: int) -> tuple[list[str], int]:
    words = [word for word in text.split() if word]
    if not words:
        return [], max_size

    size = max_size
    while size > 10:
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
    return words[:max_lines], max(10, size)


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

    fade_path = _asset_path("fadepop.png")
    if fade_path.exists():
        with Image.open(fade_path) as fade_img:
            fade = fade_img.convert("RGBA")
        scale = width / fade.width
        fade = fade.resize((width, max(1, int(fade.height * scale))), Image.Resampling.LANCZOS)
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
        lines, used_size = _wrap_text(
            title_text,
            max_size=spec["max_size"],
            max_width=width - (pad_left + pad_right),
            max_lines=3,
        )
        title_font = _load_font(used_size)
        line_gap = spec["line_gap"]
        title_baseline = height - spec["bottom_gap"]
        title_top_baseline = title_baseline - line_gap * (len(lines) - 1)

        if category:
            tag_size = max(18, int(used_size / 2))
            tag_font = _load_font(tag_size)
            left, top, right, bottom = _text_bbox(tag_font, category)
            text_width = right - left
            ascent = -top
            descent = bottom
            pad_x = 24
            pad_y = 12
            radius = 12
            cat_baseline = title_top_baseline - 14 - 50
            x1 = pad_left
            y1 = int(cat_baseline - ascent - pad_y)
            x2 = int(x1 + text_width + pad_x * 2)
            y2 = int(cat_baseline + descent + pad_y)
            _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=radius, fill=yellow)
            draw.text((x1 + pad_x - left, cat_baseline + top), category, font=tag_font, fill=black)

        current_y = title_top_baseline
        for line in lines:
            left, top, _right, _bottom = _text_bbox(title_font, line)
            draw.text((pad_left - left, current_y + top), line, font=title_font, fill=white)
            current_y += line_gap
    elif category:
        tag_font = _load_font(22)
        left, top, right, bottom = _text_bbox(tag_font, category)
        text_width = right - left
        text_height = bottom - top
        pad_x = 22
        pad_y = 10
        x1 = pad_left
        y1 = height - text_height - 90
        x2 = x1 + text_width + pad_x * 2
        y2 = y1 + text_height + pad_y * 2
        _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
        draw.text((x1 + pad_x - left, y1 + pad_y - top), category, font=tag_font, fill=black)

    return canvas


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
    media_root = Path(current_app.config["MEDIA_ROOT"]).resolve()
    target_dir = media_root / "gerador" / "generated"
    target_dir.mkdir(parents=True, exist_ok=True)

    for key, spec in VARIANT_SPECS.items():
        image = generate_art_image(title=title, image_source=image_source, variant=key, include_title=include_title, category_text=category_text)
        filename = f"arte-{key}-{uuid.uuid4().hex}.png"
        path = target_dir / filename
        image.convert("RGB").save(path, format="PNG")
        relative = path.relative_to(media_root).as_posix()
        results.append(
            {
                "key": key,
                "label": spec["label"],
                "size": f"{spec['width']}x{spec['height']}",
                "url": _media_relative_to_url(relative),
                "download_name": filename,
            }
        )

    return results
