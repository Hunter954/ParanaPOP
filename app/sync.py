from datetime import datetime
from slugify import slugify
import bleach

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

def _featured_img_from_embed(p: dict) -> str | None:
    try:
        media = p.get("_embedded", {}).get("wp:featuredmedia", [])
        if media and "source_url" in media[0]:
            return media[0]["source_url"]
    except Exception:
        return None
    return None

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

def sync_posts(client: WPClient, max_pages: int = 10, per_page: int = 20):
    # max_pages limita para evitar loop infinito
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

            # sanitiza html para evitar scripts
            excerpt_safe = bleach.clean(excerpt, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
            content_safe = bleach.clean(content, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)

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

            # categorias
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
