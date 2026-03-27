from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import mimetypes
import re
import uuid

import bleach
import requests
from flask import current_app
from slugify import slugify

from .models import db, Post, Category
from .wp_client import WPClient

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS.union({
    "p","br","hr","img","h1","h2","h3","h4","h5","h6","blockquote",
    "ul","ol","li","strong","em","a","span","div","figure","figcaption"
})
ALLOWED_ATTRS = dict(bleach.sanitizer.ALLOWED_ATTRIBUTES)
ALLOWED_ATTRS.update({
    "a": ["href","title","target","rel"],
    "img": ["src","alt","title","loading","width","height"],
    "div": ["class"],
    "span": ["class"],
    "figure": ["class"],
})

IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", re.IGNORECASE)


def _featured_img_from_embed(p: dict) -> str | None:
    try:
        media = p.get("_embedded", {}).get("wp:featuredmedia", [])
        if media and "source_url" in media[0]:
            return media[0]["source_url"]
    except Exception:
        return None
    return None


def _media_url(relative_path: str) -> str:
    prefix = current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")
    return f"{prefix}/{relative_path.lstrip('/')}"


def _guess_extension(source_url: str, content_type: str = "") -> str:
    parsed = urlparse(source_url or "")
    ext = Path(parsed.path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        return ".jpg"
    if guessed in {".jpg", ".png", ".webp", ".gif", ".svg"}:
        return guessed
    return ".jpg"


def download_external_image(source_url: str | None, folder: str = "wp") -> str | None:
    if not source_url:
        return None
    if source_url.startswith("/media/"):
        return source_url

    media_root = Path(current_app.config["MEDIA_ROOT"])
    target_dir = media_root / folder / datetime.utcnow().strftime("%Y/%m")
    target_dir.mkdir(parents=True, exist_ok=True)

    response = requests.get(source_url, timeout=25, stream=True)
    response.raise_for_status()

    ext = _guess_extension(source_url, response.headers.get("Content-Type", ""))
    filename = f"{uuid.uuid4().hex}{ext}"
    target_path = target_dir / filename

    with target_path.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)

    relative = target_path.relative_to(media_root).as_posix()
    return _media_url(relative)


def localize_content_images(html: str | None) -> str | None:
    if not html:
        return html

    cache = {}

    def repl(match):
        prefix, src, suffix = match.groups()
        if not src or src.startswith("/media/") or src.startswith("data:"):
            return match.group(0)
        if src not in cache:
            try:
                cache[src] = download_external_image(src, folder="wp/content") or src
            except Exception:
                cache[src] = src
        return f"{prefix}{cache[src]}{suffix}"

    return IMG_SRC_RE.sub(repl, html)


def sync_categories(client: WPClient):
    page = 1
    while True:
        data, _headers = client.list_categories(page=page, per_page=100)
        if not data:
            break

        for c in data:
            slug = c.get("slug") or slugify(c.get("name","cat"))
            cat = Category.query.filter_by(wp_id=c["id"]).first()
            if not cat:
                cat = Category(wp_id=c["id"], slug=slug, name=c.get("name",""))
                db.session.add(cat)
            else:
                cat.slug = slug
                cat.name = c.get("name","")

        db.session.commit()
        if len(data) < 100:
            break
        page += 1


def sync_posts(client: WPClient, max_pages: int = 10, per_page: int = 20, download_images: bool = False):
    page = 1
    while page <= max_pages:
        data, headers = client.list_posts(page=page, per_page=per_page)
        if not data:
            break

        for p in data:
            wp_id = p["id"]
            title = (p.get("title") or {}).get("rendered") or ""
            slug = p.get("slug") or slugify(title)[:200]
            excerpt = (p.get("excerpt") or {}).get("rendered") or ""
            content = (p.get("content") or {}).get("rendered") or ""
            featured = _featured_img_from_embed(p)
            date_str = p.get("date_gmt") or p.get("date")
            mod_str = p.get("modified_gmt") or p.get("modified")
            published_at = datetime.fromisoformat(date_str.replace("Z","")) if date_str else None
            updated_at = datetime.fromisoformat(mod_str.replace("Z","")) if mod_str else None

            excerpt_safe = bleach.clean(excerpt, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
            content_safe = bleach.clean(content, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
            if download_images:
                if featured and not featured.startswith('/media/'):
                    try:
                        featured = download_external_image(featured, folder='wp/featured') or featured
                    except Exception:
                        pass
                content_safe = localize_content_images(content_safe)

            post = Post.query.filter_by(wp_id=wp_id).first()
            if not post:
                post = Post(wp_id=wp_id, source="wp", slug=slug, title=title)
                db.session.add(post)

            post.title = title
            post.slug = slug
            post.excerpt = excerpt_safe
            post.content_html = content_safe
            post.featured_image = featured
            post.published_at = published_at
            post.updated_at = updated_at

            post.categories = []
            for cid in (p.get("categories") or []):
                cat = Category.query.filter_by(wp_id=cid).first()
                if cat:
                    post.categories.append(cat)

        db.session.commit()
        total_pages = int(headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1


def localize_existing_wp_images(limit: int | None = None) -> dict:
    posts_query = Post.query.filter(Post.source == 'wp').order_by(Post.published_at.desc(), Post.id.desc())
    if limit:
        posts = posts_query.limit(limit).all()
    else:
        posts = posts_query.all()

    updated = 0
    featured_downloaded = 0
    content_updated = 0
    for post in posts:
        changed = False
        if post.featured_image and not post.featured_image.startswith('/media/'):
            try:
                post.featured_image = download_external_image(post.featured_image, folder='wp/featured') or post.featured_image
                featured_downloaded += 1
                changed = True
            except Exception:
                pass

        if post.content_html and '/media/' not in post.content_html and '<img' in post.content_html.lower():
            localized = localize_content_images(post.content_html)
            if localized != post.content_html:
                post.content_html = localized
                content_updated += 1
                changed = True

        if changed:
            updated += 1

    db.session.commit()
    return {
        'updated_posts': updated,
        'featured_downloaded': featured_downloaded,
        'content_updated': content_updated,
    }
