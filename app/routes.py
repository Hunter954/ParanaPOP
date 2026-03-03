from flask import Blueprint, render_template, abort, request
from sqlalchemy import desc

from .models import db, Post, Category, AdSlot, SiteSetting, PageView

site_bp = Blueprint("site", __name__)

def _get_ad(key: str) -> str:
    slot = AdSlot.query.filter_by(key=key, is_active=True).first()
    return slot.html if slot and slot.html else ""

def _setting(key: str, default: str = "") -> str:
    s = SiteSetting.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default

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

    latest = Post.query.order_by(desc(Post.published_at)).limit(12).all()

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

    cat_brasil, posts_brasil = cat_posts("brasil", 8)
    cat_ultimas, posts_ultimas = None, latest

    # carrosseis (exemplos: entretenimento, politica, esportes)
    carousels = []
    for slug in ["entretenimento", "politica", "esportes", "parana"]:
        c, ps = cat_posts(slug, 10)
        if c and ps:
            carousels.append((c, ps))

    live_title = "AO VIVO"
    live_embed_html = _setting("live_embed_html", "")

    return render_template(
        "home.html",
        latest=latest,
        carousels=carousels,
        cat_brasil=cat_brasil,
        posts_brasil=posts_brasil,
        live_title=live_title,
        live_embed_html=live_embed_html,
        ad_header=_get_ad("header_top"),
        ad_lateral_1=_get_ad("lateral_1"),
        ad_lateral_2=_get_ad("lateral_2"),
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
        ad_lateral_1=_get_ad("lateral_1"),
        ad_lateral_2=_get_ad("lateral_2"),
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
        ad_lateral_1=_get_ad("lateral_1"),
        ad_lateral_2=_get_ad("lateral_2"),
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
        ad_lateral_1=_get_ad("lateral_1"),
        ad_lateral_2=_get_ad("lateral_2"),
    )
