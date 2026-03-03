from datetime import datetime, timedelta

from flask import Blueprint, render_template, abort, request
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
    # Navegação: lista de categorias
    cats = Category.query.order_by(Category.name.asc()).all()
    return {
        "nav_categories": cats,
        "logo_url": _setting("logo_url", ""),
    }

def _track_view(post_id=None):
    try:
        pv = PageView(
            post_id=post_id,
            path=request.path,
            ua=(request.headers.get("User-Agent") or "")[:400],
            ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:80],
        )
        db.session.add(pv)
        db.session.commit()
    except Exception:
        db.session.rollback()

@site_bp.get("/")
def home():
    _track_view(None)

    latest = Post.query.order_by(desc(Post.published_at)).limit(18).all()

    # tenta pegar categorias principais pelo slug (ajuste como quiser no admin depois)
    def cat_posts(slug, limit=6):
        cat = Category.query.filter_by(slug=slug).first()
        if not cat:
            return None, []
        posts = (Post.query.join(Post.categories)
                 .filter(Category.id == cat.id)
                 .order_by(desc(Post.published_at))
                 .limit(limit).all())
        return cat, posts

    # seção "Sports" do layout vira um bloco de categoria selecionável
    selected_cat_slug = (request.args.get("cat") or "").strip() or "esportes"
    selected_cat, selected_posts = cat_posts(selected_cat_slug, 8)

    # Popular do dia (top posts com mais pageviews nas últimas 24h)
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
    return render_template(
        "post.html",
        post=post,
        ad_header=_get_ad("header_top"),
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
