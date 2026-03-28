from datetime import datetime, timedelta
from html import unescape, escape
from pathlib import Path
import re
import json

from flask import Blueprint, render_template, abort, request, current_app, send_from_directory, jsonify, make_response, url_for
from sqlalchemy import desc, func, or_

from .models import db, Post, Category, AdSlot, SiteSetting, PageView, AnalyticsSession
from .sync import download_external_image

site_bp = Blueprint("site", __name__)


def _parse_ad_payload(raw: str | None) -> dict | None:
    value = (raw or "").strip()
    if not value.startswith("__ADCFG__"):
        return None
    try:
        payload = json.loads(value[len("__ADCFG__"):])
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


AD_SLOT_CLASSNAMES = {
    "header_top": "ad-slot-banner ad-slot-banner--wide",
    "home_top": "ad-slot-banner ad-slot-banner--wide",
    "home_mid": "ad-slot-banner ad-slot-banner--wide",
    "home_bottom": "ad-slot-banner ad-slot-banner--wide",
    "sidebar_1": "ad-slot-banner ad-slot-banner--square",
    "sidebar_2": "ad-slot-banner ad-slot-banner--square",
}


def _render_ad_from_payload(slot_key: str, payload: dict) -> str:
    banners = payload.get("banners") or []
    clean_banners = []
    for item in banners:
        if not isinstance(item, dict):
            continue
        image = (item.get("image") or "").strip()
        if not image:
            continue
        clean_banners.append({
            "image": image,
            "link": (item.get("link") or "").strip() or "#",
            "title": (item.get("title") or "").strip() or payload.get("name") or "Publicidade",
        })
    if not clean_banners:
        return ""

    seconds = payload.get("interval_seconds", 5)
    try:
        seconds = max(1, min(int(seconds), 120))
    except Exception:
        seconds = 5

    slot_id = re.sub(r'[^a-z0-9_-]+', '-', (slot_key or 'ad').lower())
    wrapper_class = AD_SLOT_CLASSNAMES.get(slot_key, 'ad-slot-banner ad-slot-banner--wide')
    slides_html = []
    for idx, item in enumerate(clean_banners):
        display = 'block' if idx == 0 else 'none'
        slides_html.append(
            f'<a href="{escape(item["link"], quote=True)}" target="_blank" rel="noopener sponsored" '
            f'class="ad-rotator__slide" data-ad-slide style="display:{display};">'
            f'<img src="{escape(item["image"], quote=True)}" alt="{escape(item["title"])}" loading="lazy"></a>'
        )
    controls = ''
    script = ''
    if len(clean_banners) > 1:
        interval_ms = seconds * 1000
        script = (
            '<script>(function(){'
            f'const root=document.getElementById("ad-rotator-{slot_id}");if(!root){{return;}}'
            'const slides=[...root.querySelectorAll("[data-ad-slide]")];'
            'if(slides.length<2){return;}let index=0;'
            'const show=(i)=>{index=i;slides.forEach((slide,n)=>{slide.style.display=n===i?"block":"none";});};'
            ''
            f'setInterval(()=>show((index+1)%slides.length),{interval_ms});'
            '})();</script>'
        )
    return f'<div class="ad-rotator {wrapper_class}" id="ad-rotator-{slot_id}">{"".join(slides_html)}{controls}</div>{script}'


def _get_ad(key: str) -> str:
    slot = AdSlot.query.filter_by(key=key, is_active=True).first()
    if not slot or not slot.html:
        return ""
    payload = _parse_ad_payload(slot.html)
    if payload:
        return _render_ad_from_payload(key, payload)
    return slot.html


def _setting(key: str, default: str = "") -> str:
    s = SiteSetting.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default


def _absolute_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return request.url_root.rstrip("/") + value


def _site_name() -> str:
    return _setting("site_name", current_app.config.get("SITE_NAME", "News"))


def _site_tagline() -> str:
    return _setting("site_tagline", "Portal de notícias do Oeste do Paraná")


def _default_share_image() -> str:
    return _absolute_url(_setting("default_share_image", _setting("logo_url", "")))


def _meta_defaults():
    return {
        "site_name_value": _site_name(),
        "favicon_url": _setting("favicon_url", ""),
        "meta_title": _site_name(),
        "meta_description": _setting("default_meta_description", _site_tagline()),
        "meta_keywords": _setting("site_keywords", ""),
        "meta_image": _default_share_image(),
        "meta_url": request.url,
        "meta_type": "website",
        "facebook_app_id": _setting("facebook_app_id", ""),
        "google_site_verification": _setting("google_site_verification", ""),
        "google_analytics_id": _setting("google_analytics_id", ""),
        "social_links": {
            "instagram": _setting("instagram_url", ""),
            "facebook": _setting("facebook_url", ""),
            "youtube": _setting("youtube_url", ""),
            "x": _setting("x_url", ""),
        },
        "organization_schema": {
            "name": _site_name(),
            "url": request.url_root.rstrip("/"),
            "logo": _absolute_url(_setting("logo_url", "")) or _default_share_image(),
            "email": _setting("contact_email", ""),
            "telephone": _setting("contact_phone", ""),
        },
    }



def _nav_categories():
    selected_raw = _setting("top_menu_category_ids", "")
    if selected_raw.strip():
        try:
            selected_ids = [int(item) for item in json.loads(selected_raw) if str(item).strip()]
        except Exception:
            selected_ids = []
        if selected_ids:
            items = Category.query.filter(Category.id.in_(selected_ids)).all()
            order = {cid: idx for idx, cid in enumerate(selected_ids)}
            items.sort(key=lambda item: (order.get(item.id, 9999), item.name.lower()))
            return items
    return Category.query.order_by(Category.name.asc()).all()

@site_bp.app_context_processor
def inject_site_globals():
    cats = _nav_categories()
    return {
        "nav_categories": cats,
        "logo_url": _setting("logo_url", ""),
        "site_name_value": _site_name(),
        "favicon_url": _setting("favicon_url", ""),
        "ad_home_bottom": _get_ad("home_bottom"),
        "clean_text": _clean_text,
        "format_date_br": _format_date_br,
        "display_category_name": _display_category_name,
    }


@site_bp.get("/media/<path:filename>")
def media(filename):
    media_root = Path(current_app.config["MEDIA_ROOT"]).resolve()
    return send_from_directory(media_root, filename)



def _clean_text(value: str, limit: int = 0) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].strip()
        return (cut or text[:limit]).rstrip(" .,;:-") + "..."
    return text



def _format_date_br(value):
    if not value:
        return ""
    months = [
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    ]
    return f"{value.day} de {months[value.month - 1]} de {value.year}"



def _display_category_name(value: str, max_len: int = 18) -> str:
    if not value:
        return "Notícias"
    text = re.sub(r"\s+", " ", unescape(value)).strip()
    if len(text) <= max_len:
        return text
    for token in [" contra ", " e ", " / ", " | ", " - "]:
        if token in text.lower():
            idx = text.lower().find(token)
            short = text[:idx].strip()
            if short:
                return short
    first = text.split()[0].strip()
    return first or text[:max_len].strip()




def _parse_iso_datetime(value: str | None):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', ''))
    except Exception:
        return None


def _hub_token_is_valid() -> bool:
    expected = (_setting('hub_receive_token', '') or '').strip()
    incoming = (request.headers.get('X-Hub-Token') or '').strip()
    return bool(expected and incoming and incoming == expected)


def _published_posts_query():
    return Post.query.filter(Post.published_at.isnot(None))

def _track_view(post_id=None):
    try:
        pv = PageView(
            post_id=post_id,
            path=request.path,
            ua=(request.headers.get("User-Agent") or "")[:400],
            ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:80],
            created_at=datetime.utcnow(),
        )
        db.session.add(pv)
        db.session.commit()
    except Exception:
        db.session.rollback()


@site_bp.post("/analytics/collect")
def analytics_collect():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session_id") or "").strip()[:120]
    visitor_id = (payload.get("visitor_id") or "").strip()[:120]
    if not session_id or not visitor_id:
        return jsonify({"ok": False}), 400

    event = (payload.get("event") or "pageview").strip()
    page_path = (payload.get("page_path") or request.path or "/").strip()[:800]
    referrer = (payload.get("referrer") or request.referrer or "").strip()[:800]
    duration = max(0, min(int(payload.get("duration_seconds") or 0), 7200))
    is_new_user = bool(payload.get("is_new_user"))

    try:
        session = AnalyticsSession.query.filter_by(session_id=session_id).first()
        now = datetime.utcnow()
        if not session:
            session = AnalyticsSession(
                session_id=session_id,
                visitor_id=visitor_id,
                landing_path=page_path,
                referrer=referrer,
                user_agent=(request.headers.get("User-Agent") or "")[:400],
                pageviews=1,
                duration_seconds=duration if event == "heartbeat" else 0,
                is_bounce=True,
                is_new_user=is_new_user,
                created_at=now,
                updated_at=now,
            )
            db.session.add(session)
        else:
            if event == "pageview":
                session.pageviews = max(1, (session.pageviews or 0) + 1)
                session.is_bounce = session.pageviews <= 1
            if event == "heartbeat":
                session.duration_seconds = max(session.duration_seconds or 0, duration)
                if duration >= 10 or (session.pageviews or 0) > 1:
                    session.is_bounce = False
            session.updated_at = now
            if not session.referrer and referrer:
                session.referrer = referrer
        db.session.commit()
        return jsonify({"ok": True})
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False}), 500


@site_bp.get("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {request.url_root.rstrip('/')}{url_for('site.sitemap_xml')}",
    ]
    response = make_response("\n".join(lines))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response


@site_bp.get("/sitemap.xml")
def sitemap_xml():
    pages = [
        (url_for('site.home', _external=True), datetime.utcnow()),
        (url_for('site.search', _external=True), datetime.utcnow()),
    ]
    for cat in Category.query.order_by(Category.updated_at.desc() if hasattr(Category, 'updated_at') else Category.id.desc()).all():
        pages.append((url_for('site.category', slug=cat.slug, _external=True), datetime.utcnow()))
    for post in _published_posts_query().order_by(desc(Post.updated_at), desc(Post.published_at)).limit(2000).all():
        pages.append((url_for('site.post', slug=post.slug, _external=True), post.updated_at or post.published_at or datetime.utcnow()))
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in pages:
        xml.append('<url>')
        xml.append(f'<loc>{loc}</loc>')
        xml.append(f'<lastmod>{lastmod.date().isoformat()}</lastmod>')
        xml.append('</url>')
    xml.append('</urlset>')
    response = make_response(''.join(xml))
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return response




@site_bp.post('/api/hub/posts/upsert')
def hub_posts_upsert_api():
    if not _hub_token_is_valid():
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    post_data = payload.get('post') or {}
    slug = (post_data.get('slug') or '').strip()
    title = (post_data.get('title') or '').strip()
    if not slug or not title:
        return jsonify({'ok': False, 'error': 'missing_slug_or_title'}), 400

    post = _published_posts_query().filter_by(slug=slug).first()
    if not post:
        post = Post(slug=slug, title=title, source='hub')
        db.session.add(post)

    post.title = title
    post.excerpt = post_data.get('excerpt') or ''
    post.content_html = post_data.get('content_html') or ''
    post.author_name = (post_data.get('author_name') or '').strip() or 'Redação'
    post.published_at = _parse_iso_datetime(post_data.get('published_at')) or post.published_at or datetime.utcnow()
    post.updated_at = _parse_iso_datetime(post_data.get('updated_at')) or datetime.utcnow()
    post.source = 'hub'

    incoming_image = (post_data.get('featured_image') or '').strip()
    if incoming_image:
        try:
            post.featured_image = download_external_image(incoming_image, folder='hub/featured') or incoming_image
        except Exception:
            post.featured_image = incoming_image

    categories = []
    for item in (post_data.get('categories') or []):
        if not isinstance(item, dict):
            continue
        name = (item.get('name') or '').strip()
        slug_value = (item.get('slug') or '').strip()
        if not name or not slug_value:
            continue
        cat = Category.query.filter_by(slug=slug_value).first()
        if not cat:
            cat = Category(name=name, slug=slug_value)
            db.session.add(cat)
            db.session.flush()
        else:
            cat.name = name
        categories.append(cat)
    post.categories = categories
    db.session.commit()
    return jsonify({'ok': True, 'slug': post.slug})


@site_bp.post('/api/hub/posts/delete')
def hub_posts_delete_api():
    if not _hub_token_is_valid():
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    slug = (payload.get('slug') or '').strip()
    if not slug:
        return jsonify({'ok': False, 'error': 'missing_slug'}), 400
    post = _published_posts_query().filter_by(slug=slug).first()
    if not post:
        return jsonify({'ok': True, 'deleted': False})
    db.session.delete(post)
    db.session.commit()
    return jsonify({'ok': True, 'deleted': True})



@site_bp.get("/")
def home():
    _track_view(None)

    latest = _published_posts_query().order_by(desc(Post.published_at), desc(Post.id)).limit(24).all()
    lead_post = latest[0] if latest else None
    latest_queue = latest[1:4] if len(latest) > 1 else []
    excluded_ids = {p.id for p in [lead_post, *latest_queue] if p}

    def cat_posts(slug, limit=6, exclude_ids=None):
        cat = Category.query.filter_by(slug=slug).first()
        if not cat:
            return None, []
        q = (_published_posts_query().join(Post.categories)
             .filter(Category.id == cat.id))
        if exclude_ids:
            q = q.filter(~Post.id.in_(list(exclude_ids)))
        posts = q.order_by(desc(Post.published_at), desc(Post.id)).limit(limit).all()
        return cat, posts

    brasil_cat, brasil_posts = cat_posts("brasil", 6, excluded_ids)

    selected_cat_slug = (request.args.get("cat") or "").strip() or "cidade"
    selected_cat, selected_posts = cat_posts(selected_cat_slug, 8)

    category_sections = []
    for cat in Category.query.order_by(Category.name.asc()).limit(8).all():
        posts = (_published_posts_query().join(Post.categories)
                 .filter(Category.id == cat.id)
                 .order_by(desc(Post.published_at), desc(Post.id))
                 .limit(6).all())
        if posts:
            category_sections.append({"category": cat, "posts": posts})

    if not selected_cat and category_sections:
        selected_cat = category_sections[0]["category"]
        selected_posts = category_sections[0]["posts"]
        selected_cat_slug = selected_cat.slug

    since = datetime.utcnow() - timedelta(hours=24)
    popular_ids = (
        db.session.query(PageView.post_id, func.count(PageView.id).label("c"))
        .filter(PageView.post_id.isnot(None))
        .filter(PageView.created_at >= since)
        .group_by(PageView.post_id)
        .order_by(desc("c"))
        .limit(5)
        .all()
    )
    popular_map = {pid: c for pid, c in popular_ids if pid}
    popular_posts = []
    if popular_map:
        posts = _published_posts_query().filter(Post.id.in_(list(popular_map.keys()))).all()
        posts_by_id = {p.id: p for p in posts}
        popular_posts = [posts_by_id[pid] for pid, _ in popular_ids if pid in posts_by_id]

    live_title = "AO VIVO"
    live_embed_html = _setting("live_embed_html", "")

    return render_template(
        "home.html",
        **_meta_defaults(),
        latest=latest,
        lead_post=lead_post,
        latest_queue=latest_queue,
        brasil_posts=brasil_posts,
        brasil_category=brasil_cat,
        selected_cat=selected_cat,
        selected_posts=selected_posts,
        popular_posts=popular_posts,
        selected_cat_slug=selected_cat_slug,
        category_sections=category_sections,
        live_title=live_title,
        live_embed_html=live_embed_html,
        ad_header=_get_ad("header_top"),
        ad_home_middle=_get_ad("home_top"),
        ad_article_end=_get_ad("home_mid"),
        ad_home_bottom=_get_ad("home_bottom"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )


@site_bp.get("/p/<slug>")
def post(slug):
    post = _published_posts_query().filter_by(slug=slug).first()
    if not post:
        abort(404)
    _track_view(post.id)

    category_ids = [c.id for c in post.categories]
    latest_posts = (_published_posts_query()
                    .filter(Post.id != post.id)
                    .order_by(desc(Post.published_at))
                    .limit(4)
                    .all())

    related_posts = []
    if category_ids:
        related_posts = (_published_posts_query().join(Post.categories)
                         .filter(Category.id.in_(category_ids), Post.id != post.id)
                         .order_by(desc(Post.published_at))
                         .limit(6)
                         .all())

    if len(related_posts) < 6:
        existing_ids = {p.id for p in related_posts}
        existing_ids.add(post.id)
        complement = (_published_posts_query()
                      .filter(~Post.id.in_(list(existing_ids)))
                      .order_by(desc(Post.published_at))
                      .limit(6 - len(related_posts))
                      .all())
        related_posts.extend(complement)

    related_label = post.categories[0].name if post.categories else "Notícias"

    meta = _meta_defaults()
    meta.update({
        "meta_title": _clean_text(post.title, 110),
        "meta_description": _clean_text(post.excerpt or post.content_html, 170) or _setting("default_meta_description", _site_tagline()),
        "meta_keywords": ", ".join([c.name for c in post.categories]) or _setting("site_keywords", ""),
        "meta_image": _absolute_url(post.featured_image or _setting("default_share_image", _setting("logo_url", ""))),
        "meta_url": url_for("site.post", slug=post.slug, _external=True),
        "meta_type": "article",
        "article_published_time": post.published_at.isoformat() if post.published_at else "",
        "article_modified_time": (post.updated_at or post.published_at).isoformat() if (post.updated_at or post.published_at) else "",
    })

    return render_template(
        "post.html",
        post=post,
        **meta,
        latest_posts=latest_posts,
        related_posts=related_posts,
        related_label=related_label,
        ad_header=_get_ad("header_top"),
        ad_article_end=_get_ad("home_mid"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )


@site_bp.get("/c/<slug>")
def category(slug):
    cat = Category.query.filter_by(slug=slug).first()
    if not cat:
        abort(404)

    page = max(int(request.args.get("page", "1")), 1)
    per_page = 12
    q = (_published_posts_query().join(Post.categories)
         .filter(Category.id == cat.id)
         .order_by(desc(Post.published_at)))
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    _track_view(None)

    meta = _meta_defaults()
    meta.update({
        "meta_title": f"{cat.name} | {_site_name()}",
        "meta_description": f"Últimas matérias da editoria {cat.name} no {_site_name()}.",
        "meta_url": url_for("site.category", slug=cat.slug, _external=True),
    })
    return render_template(
        "category.html",
        cat=cat,
        **meta,
        pagination=pagination,
        ad_header=_get_ad("header_top"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )


@site_bp.get("/buscar")
def search():
    term = (request.args.get("q") or "").strip()
    page = max(int(request.args.get("page", "1")), 1)
    per_page = 12

    q = _published_posts_query()
    if term:
        like = f"%{term}%"
        q = q.filter(Post.title.ilike(like))
    q = q.order_by(desc(Post.published_at))
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    _track_view(None)

    meta = _meta_defaults()
    meta.update({
        "meta_title": f"Buscar{' - ' + term if term else ''} | {_site_name()}",
        "meta_description": f"Busca de notícias{' sobre ' + term if term else ''} no {_site_name()}.",
        "meta_url": url_for("site.search", q=term, _external=True) if term else url_for("site.search", _external=True),
    })
    return render_template(
        "search.html",
        term=term,
        **meta,
        pagination=pagination,
        ad_header=_get_ad("header_top"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )
