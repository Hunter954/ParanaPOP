from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
import re

from flask import Blueprint, render_template, abort, request, current_app, send_from_directory
from sqlalchemy import desc, func

from .models import db, Post, Category, AdSlot, SiteSetting, PageView

site_bp = Blueprint("site", __name__)


def _get_ad(key: str) -> str:
    slot = AdSlot.query.filter_by(key=key, is_active=True).first()
    return slot.html if slot and slot.html else ""


def _setting(key: str, default: str = "") -> str:
    s = SiteSetting.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default


@site_bp.app_context_processor
def inject_site_globals():
    cats = Category.query.order_by(Category.name.asc()).all()
    return {
        "nav_categories": cats,
        "logo_url": _setting("logo_url", ""),
        "clean_text": _clean_text,
        "format_date_br": _format_date_br,
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


@site_bp.get("/")
def home():
    _track_view(None)

    latest = Post.query.order_by(desc(Post.published_at)).limit(18).all()

    def cat_posts(slug, limit=6):
        cat = Category.query.filter_by(slug=slug).first()
        if not cat:
            return None, []
        posts = (Post.query.join(Post.categories)
                 .filter(Category.id == cat.id)
                 .order_by(desc(Post.published_at))
                 .limit(limit).all())
        return cat, posts

    selected_cat_slug = (request.args.get("cat") or "").strip() or "cidade"
    selected_cat, selected_posts = cat_posts(selected_cat_slug, 8)

    category_sections = []
    for cat in Category.query.order_by(Category.name.asc()).limit(8).all():
        posts = (Post.query.join(Post.categories)
                 .filter(Category.id == cat.id)
                 .order_by(desc(Post.published_at))
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
        posts = Post.query.filter(Post.id.in_(list(popular_map.keys()))).all()
        posts_by_id = {p.id: p for p in posts}
        popular_posts = [posts_by_id[pid] for pid, _ in popular_ids if pid in posts_by_id]

    live_title = "AO VIVO"
    live_embed_html = _setting("live_embed_html", "")

    return render_template(
        "home.html",
        latest=latest,
        selected_cat=selected_cat,
        selected_posts=selected_posts,
        popular_posts=popular_posts,
        selected_cat_slug=selected_cat_slug,
        category_sections=category_sections,
        live_title=live_title,
        live_embed_html=live_embed_html,
        ad_header=_get_ad("header_top"),
        ad_home_top=_get_ad("home_top"),
        ad_home_mid=_get_ad("home_mid"),
        ad_home_bottom=_get_ad("home_bottom"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )


@site_bp.get("/p/<slug>")
def post(slug):
    post = Post.query.filter_by(slug=slug).first()
    if not post:
        abort(404)
    _track_view(post.id)

    category_ids = [c.id for c in post.categories]
    latest_posts = (Post.query
                    .filter(Post.id != post.id)
                    .order_by(desc(Post.published_at))
                    .limit(4)
                    .all())

    related_posts = []
    if category_ids:
        related_posts = (Post.query.join(Post.categories)
                         .filter(Category.id.in_(category_ids), Post.id != post.id)
                         .order_by(desc(Post.published_at))
                         .limit(6)
                         .all())

    if len(related_posts) < 6:
        existing_ids = {p.id for p in related_posts}
        existing_ids.add(post.id)
        complement = (Post.query
                      .filter(~Post.id.in_(list(existing_ids)))
                      .order_by(desc(Post.published_at))
                      .limit(6 - len(related_posts))
                      .all())
        related_posts.extend(complement)

    related_label = post.categories[0].name if post.categories else "Notícias"

    return render_template(
        "post.html",
        post=post,
        latest_posts=latest_posts,
        related_posts=related_posts,
        related_label=related_label,
        ad_header=_get_ad("header_top"),
        ad_home_mid=_get_ad("home_mid"),
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
    q = (Post.query.join(Post.categories)
         .filter(Category.id == cat.id)
         .order_by(desc(Post.published_at)))
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    _track_view(None)

    return render_template(
        "category.html",
        cat=cat,
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

    q = Post.query
    if term:
        like = f"%{term}%"
        q = q.filter(Post.title.ilike(like))
    q = q.order_by(desc(Post.published_at))
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    _track_view(None)

    return render_template(
        "search.html",
        term=term,
        pagination=pagination,
        ad_header=_get_ad("header_top"),
        ad_sidebar_1=_get_ad("sidebar_1"),
        ad_sidebar_2=_get_ad("sidebar_2"),
    )
