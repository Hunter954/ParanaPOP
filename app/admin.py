import os
import re
import shutil
import json
import requests
from datetime import datetime, timedelta, date, time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, abort
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func, desc
from werkzeug.utils import secure_filename

from .models import db, User, AdSlot, SiteSetting, PageView, Post, Category, post_categories, AnalyticsSession
from .sync import download_external_image
from .forms import LoginForm, AdSlotForm, CategoryForm, PostAdminForm
from .wp_client import WPClient
from .sync import sync_categories, sync_posts, localize_existing_wp_images

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _require_admin():
    if not current_user.is_authenticated:
        return redirect(url_for("admin.login"))
    if not getattr(current_user, "is_admin", False):
        flash("Acesso negado.", "danger")
        return redirect(url_for("site.home"))
    return None


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9à-úçãõâêîôûäëïöü\s-]", "", value)
    repl = {
        "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a",
        "é": "e", "ê": "e", "ë": "e",
        "í": "i", "î": "i", "ï": "i",
        "ó": "o", "ô": "o", "õ": "o", "ö": "o",
        "ú": "u", "û": "u", "ü": "u",
        "ç": "c",
    }
    for src, dst in repl.items():
        value = value.replace(src, dst)
    value = re.sub(r"[\s_-]+", "-", value)
    return value.strip("-") or f"item-{uuid4().hex[:8]}"


def _ensure_unique_slug(model, desired: str, object_id=None) -> str:
    base = _slugify(desired)
    slug = base
    i = 2
    while True:
        q = model.query.filter_by(slug=slug)
        obj = q.first()
        if not obj or (object_id and getattr(obj, "id", None) == object_id):
            return slug
        slug = f"{base}-{i}"
        i += 1


def _setting(key: str, default: str = "") -> str:
    s = SiteSetting.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default


def _save_setting(key: str, value: str) -> None:
    s = SiteSetting.query.filter_by(key=key).first()
    if not s:
        s = SiteSetting(key=key, value=value)
        db.session.add(s)
    else:
        s.value = value


def _setting_bool(key: str, default: bool = False) -> bool:
    raw = (_setting(key, '1' if default else '0') or '').strip().lower()
    return raw in {'1', 'true', 'yes', 'on', 'sim'}


def _setting_json(key: str, default):
    raw = (_setting(key, '') or '').strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _absolute_media_url(value: str) -> str:
    if not value:
        return ''
    if value.startswith('http://') or value.startswith('https://'):
        return value
    base = request.url_root.rstrip('/')
    return f"{base}{value if value.startswith('/') else '/' + value}"


def _hub_config():
    remotes = _setting_json('hub_remote_sites_json', [])
    if not isinstance(remotes, list):
        remotes = []
    cleaned = []
    for item in remotes:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            'name': (item.get('name') or '').strip(),
            'site_key': (item.get('site_key') or '').strip(),
            'base_url': (item.get('base_url') or '').strip().rstrip('/'),
            'api_token': (item.get('api_token') or '').strip(),
            'active': bool(item.get('active')),
        })
    return {
        'enabled': _setting_bool('hub_enabled', False),
        'site_key': (_setting('hub_site_key', '') or '').strip(),
        'site_name': (_setting('site_name', current_app.config.get('SITE_NAME', 'News')) or '').strip(),
        'receive_token': (_setting('hub_receive_token', '') or '').strip(),
        'auto_push': _setting_bool('hub_auto_push', True),
        'remotes': cleaned,
    }


def _serialize_post_for_hub(post: Post) -> dict:
    return {
        'site_key': (_setting('hub_site_key', '') or '').strip(),
        'site_name': (_setting('site_name', current_app.config.get('SITE_NAME', 'News')) or '').strip(),
        'post': {
            'title': post.title or '',
            'slug': post.slug or '',
            'excerpt': post.excerpt or '',
            'content_html': post.content_html or '',
            'featured_image': _absolute_media_url(post.featured_image or ''),
            'author_name': post.author_name or 'Anônimo',
            'published_at': post.published_at.isoformat() if post.published_at else '',
            'updated_at': (post.updated_at or datetime.utcnow()).isoformat(),
            'categories': [{'name': c.name, 'slug': c.slug} for c in (post.categories or [])],
            'source': post.source or 'local',
            'is_draft': bool(post.is_draft),
        }
    }


def _push_post_to_remote(post: Post, remote: dict) -> tuple[bool, str]:
    base_url = (remote.get('base_url') or '').strip().rstrip('/')
    token = (remote.get('api_token') or '').strip()
    if not base_url or not token:
        return False, 'URL ou token ausentes'
    try:
        response = requests.post(
            f"{base_url}/api/hub/posts/upsert",
            json=_serialize_post_for_hub(post),
            headers={'X-Hub-Token': token, 'Content-Type': 'application/json'},
            timeout=45,
        )
        if 200 <= response.status_code < 300:
            return True, 'OK'
        return False, f'HTTP {response.status_code}'
    except Exception as exc:
        return False, str(exc)[:180]


def _push_delete_to_remote(post: Post, remote: dict) -> tuple[bool, str]:
    base_url = (remote.get('base_url') or '').strip().rstrip('/')
    token = (remote.get('api_token') or '').strip()
    if not base_url or not token:
        return False, 'URL ou token ausentes'
    try:
        response = requests.post(
            f"{base_url}/api/hub/posts/delete",
            json={'slug': post.slug, 'site_key': (_setting('hub_site_key', '') or '').strip()},
            headers={'X-Hub-Token': token, 'Content-Type': 'application/json'},
            timeout=30,
        )
        if 200 <= response.status_code < 300:
            return True, 'OK'
        return False, f'HTTP {response.status_code}'
    except Exception as exc:
        return False, str(exc)[:180]


def _broadcast_post_to_hub(post: Post) -> dict:
    cfg = _hub_config()
    remotes = [item for item in cfg['remotes'] if item.get('active') and item.get('base_url')]
    results = []
    if not cfg['enabled'] or not remotes:
        return {'sent': 0, 'ok': 0, 'results': results}
    for remote in remotes:
        ok, message = _push_post_to_remote(post, remote)
        results.append({'name': remote.get('name') or remote.get('base_url'), 'ok': ok, 'message': message})
    return {'sent': len(results), 'ok': sum(1 for r in results if r['ok']), 'results': results}


def _broadcast_delete_to_hub(post: Post) -> dict:
    cfg = _hub_config()
    remotes = [item for item in cfg['remotes'] if item.get('active') and item.get('base_url')]
    results = []
    if not cfg['enabled'] or not remotes:
        return {'sent': 0, 'ok': 0, 'results': results}
    for remote in remotes:
        ok, message = _push_delete_to_remote(post, remote)
        results.append({'name': remote.get('name') or remote.get('base_url'), 'ok': ok, 'message': message})
    return {'sent': len(results), 'ok': sum(1 for r in results if r['ok']), 'results': results}


def _flash_hub_result(action_label: str, result: dict) -> None:
    sent = int(result.get('sent', 0) or 0)
    ok = int(result.get('ok', 0) or 0)
    if not sent:
        return
    if ok == sent:
        flash(f'{action_label}: sincronizado com {ok} site(s).', 'success')
        return
    fails = [f"{item['name']} ({item['message']})" for item in result.get('results', []) if not item.get('ok')]
    flash(f"{action_label}: {ok}/{sent} site(s) sincronizados. Falhas: {'; '.join(fails[:3])}", 'warning')


def _parse_remote_sites_from_form(form) -> list[dict]:
    remotes = []
    for idx in range(1, 4):
        name = (form.get(f'remote_name_{idx}') or '').strip()
        site_key = (form.get(f'remote_site_key_{idx}') or '').strip()
        base_url = (form.get(f'remote_base_url_{idx}') or '').strip().rstrip('/')
        api_token = (form.get(f'remote_api_token_{idx}') or '').strip()
        active = bool(form.get(f'remote_active_{idx}'))
        if name or site_key or base_url or api_token:
            remotes.append({
                'name': name or f'Site {idx}',
                'site_key': site_key,
                'base_url': base_url,
                'api_token': api_token,
                'active': active,
            })
    return remotes


def _media_root() -> Path:
    path = Path(current_app.config["MEDIA_ROOT"]).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_ext(filename: str) -> str:
    name = secure_filename(filename or "")
    _, ext = os.path.splitext(name)
    return ext.lower()


def _save_upload(file_storage, subdir: str = "general") -> str:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return ""
    ext = _file_ext(file_storage.filename) or ".bin"
    day_dir = datetime.utcnow().strftime("%Y/%m/%d")
    folder = _media_root() / subdir / day_dir
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid4().hex}{ext}"
    full_path = folder / fname
    file_storage.save(full_path)
    rel = full_path.relative_to(_media_root()).as_posix()
    return f"{current_app.config['MEDIA_URL_PREFIX'].rstrip('/')}/{rel}"


def _local_media_path_from_url(url: str) -> Path | None:
    if not url:
        return None
    prefix = current_app.config["MEDIA_URL_PREFIX"].rstrip("/") + "/"
    parsed = urlparse(url)
    target_path = parsed.path or url
    if not target_path.startswith(prefix):
        return None
    rel = target_path[len(prefix):].lstrip("/")
    return _media_root() / rel


def _delete_local_media(url: str) -> None:
    path = _local_media_path_from_url(url)
    if path and path.exists() and path.is_file():
        try:
            path.unlink()
        except Exception:
            pass




def _parse_date_input(value: str | None, fallback: date) -> date:
    raw = (value or '').strip()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return fallback


def _build_chart_data(daily_series, metric_key: str):
    width = 980
    height = 360
    pad_left = 56
    pad_right = 18
    pad_top = 20
    pad_bottom = 44
    inner_w = max(1, width - pad_left - pad_right)
    inner_h = max(1, height - pad_top - pad_bottom)

    values = [int(day.get(metric_key, 0) or 0) for day in daily_series]
    max_value = max(values) if values else 0
    if max_value <= 0:
        max_value = 1
    y_max = max_value
    if y_max <= 5:
        step = 1
    elif y_max <= 25:
        step = 5
    elif y_max <= 100:
        step = 10
    else:
        rough = y_max / 4
        magnitude = 10 ** max(0, len(str(int(rough))) - 1)
        step = max(10, round(rough / magnitude) * magnitude)
    y_max = ((max_value + step - 1) // step) * step
    y_ticks = list(range(0, y_max + step, step))

    count = len(daily_series)
    points = []
    circles = []
    x_labels = []
    for idx, day in enumerate(daily_series):
        x = pad_left if count <= 1 else pad_left + (inner_w * idx / (count - 1))
        value = int(day.get(metric_key, 0) or 0)
        y = pad_top + inner_h - ((value / y_max) * inner_h if y_max else 0)
        x = round(x, 2)
        y = round(y, 2)
        points.append(f"{x},{y}")
        circles.append({"cx": x, "cy": y, "value": value, "label": day.get("label", ''), "iso": day.get('iso', '')})

        show_label = count <= 10 or idx in {0, count - 1} or idx % max(1, round(count / 6)) == 0
        if show_label:
            x_labels.append({"x": x, "label": day.get("label_short", day.get("label", ''))})

    y_grid = []
    for tick in y_ticks:
        y = round(pad_top + inner_h - ((tick / y_max) * inner_h if y_max else 0), 2)
        y_grid.append({"y": y, "value": tick})

    return {
        "width": width,
        "height": height,
        "polyline": ' '.join(points),
        "circles": circles,
        "y_grid": y_grid,
        "x_labels": x_labels,
        "baseline": pad_top + inner_h,
        "pad_left": pad_left,
        "pad_right": pad_right,
    }

def _pct_delta(current: float, previous: float) -> float:
    if not previous:
        return 0.0 if not current else 100.0
    return round(((current - previous) / previous) * 100, 1)




def _analytics_stats(days: int = 30, start_date: date | None = None, end_date: date | None = None):
    today = datetime.utcnow().date()
    end_day = end_date or today
    start_day = start_date or (end_day - timedelta(days=max(days - 1, 0)))
    if start_day > end_day:
        start_day, end_day = end_day, start_day

    window_days = max((end_day - start_day).days + 1, 1)
    current_start = datetime.combine(start_day, time.min)
    current_end = datetime.combine(end_day + timedelta(days=1), time.min)
    previous_start = current_start - timedelta(days=window_days)
    previous_end = current_start

    current_sessions = AnalyticsSession.query.filter(
        AnalyticsSession.created_at >= current_start,
        AnalyticsSession.created_at < current_end,
    ).all()
    previous_sessions = AnalyticsSession.query.filter(
        AnalyticsSession.created_at >= previous_start,
        AnalyticsSession.created_at < previous_end,
    ).all()

    def summarize(items):
        sessions = len(items)
        pageviews = sum((item.pageviews or 0) for item in items)
        total_users = len({item.visitor_id for item in items if item.visitor_id})
        bounce_sessions = sum(1 for item in items if item.is_bounce)
        new_users = len({item.visitor_id for item in items if item.is_new_user and item.visitor_id})
        avg_duration = round(sum((item.duration_seconds or 0) for item in items) / sessions) if sessions else 0
        bounce_rate = round((bounce_sessions / sessions) * 100, 1) if sessions else 0
        return {
            "sessions": sessions,
            "pageviews": pageviews,
            "avg_duration": avg_duration,
            "total_users": total_users,
            "bounce_rate": bounce_rate,
            "new_users": new_users,
        }

    current = summarize(current_sessions)
    previous = summarize(previous_sessions)
    cards = []
    labels = {
        "sessions": "Sessions",
        "pageviews": "Pageviews",
        "avg_duration": "Avg. Session Duration",
        "total_users": "Total Users",
        "bounce_rate": "Bounce Rate",
        "new_users": "New Users",
    }
    for key, label in labels.items():
        cards.append({
            "key": key,
            "label": label,
            "value": current[key],
            "delta": _pct_delta(current[key], previous[key]),
        })

    top_pages = (
        db.session.query(
            AnalyticsSession.landing_path,
            func.count(AnalyticsSession.id).label("sessions"),
            func.sum(AnalyticsSession.pageviews).label("pageviews"),
        )
        .filter(AnalyticsSession.created_at >= current_start, AnalyticsSession.created_at < current_end)
        .group_by(AnalyticsSession.landing_path)
        .order_by(desc("pageviews"), desc("sessions"))
        .limit(12)
        .all()
    )

    top_referrers = (
        db.session.query(AnalyticsSession.referrer, func.count(AnalyticsSession.id).label("sessions"))
        .filter(
            AnalyticsSession.created_at >= current_start,
            AnalyticsSession.created_at < current_end,
            AnalyticsSession.referrer.isnot(None),
            AnalyticsSession.referrer != "",
        )
        .group_by(AnalyticsSession.referrer)
        .order_by(desc("sessions"))
        .limit(8)
        .all()
    )

    by_day = {}
    for offset in range(window_days):
        day = start_day + timedelta(days=offset)
        by_day[day] = {"sessions": 0, "pageviews": 0, "visitor_ids": set()}

    for item in current_sessions:
        item_day = item.created_at.date()
        if item_day not in by_day:
            continue
        bucket = by_day[item_day]
        bucket["sessions"] += 1
        bucket["pageviews"] += int(item.pageviews or 0)
        if item.visitor_id:
            bucket["visitor_ids"].add(item.visitor_id)

    daily_series = []
    for day in sorted(by_day.keys()):
        bucket = by_day[day]
        daily_series.append({
            "iso": day.isoformat(),
            "label": day.strftime('%d/%m'),
            "label_short": day.strftime('%d/%m'),
            "sessions": bucket["sessions"],
            "pageviews": bucket["pageviews"],
            "total_users": len(bucket["visitor_ids"]),
        })

    return {
        "cards": cards,
        "current": current,
        "previous": previous,
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "window_days": window_days,
        "start_date": start_day,
        "end_date": end_day,
        "daily_series": daily_series,
    }


def _dashboard_stats():
    pv_total = db.session.query(func.count(PageView.id)).scalar() or 0
    since = datetime.utcnow() - timedelta(hours=24)
    pv_24h = db.session.query(func.count(PageView.id)).filter(PageView.created_at >= since).scalar() or 0
    posts_total = db.session.query(func.count(Post.id)).scalar() or 0
    local_posts = db.session.query(func.count(Post.id)).filter(Post.source == "local").scalar() or 0
    wp_posts = db.session.query(func.count(Post.id)).filter(Post.source == "wp").scalar() or 0
    categories_total = db.session.query(func.count(Category.id)).scalar() or 0
    active_ads = db.session.query(func.count(AdSlot.id)).filter(AdSlot.is_active.is_(True)).scalar() or 0
    recent_posts = Post.query.order_by(desc(Post.updated_at), desc(Post.published_at)).limit(8).all()
    popular_posts = (
        db.session.query(Post, func.count(PageView.id).label("views"))
        .outerjoin(PageView, PageView.post_id == Post.id)
        .group_by(Post.id)
        .order_by(desc("views"), desc(Post.published_at))
        .limit(8)
        .all()
    )
    analytics = _analytics_stats(30)
    return {
        "pv_total": pv_total,
        "pv_24h": pv_24h,
        "posts_total": posts_total,
        "local_posts": local_posts,
        "wp_posts": wp_posts,
        "categories_total": categories_total,
        "active_ads": active_ads,
        "recent_posts": recent_posts,
        "popular_posts": popular_posts,
        "analytics": analytics,
    }


def _common_admin_context(section: str, **extra):
    data = {
        "section": section,
        "stats": _dashboard_stats(),
    }
    data.update(extra)
    return data


def _bind_post_form_choices(form: PostAdminForm):
    form.categories.choices = [(c.id, c.name) for c in Category.query.order_by(Category.name.asc()).all()]


def _fill_post_form_from_obj(form: PostAdminForm, post: Post):
    form.title.data = post.title
    form.excerpt.data = post.excerpt
    form.content_html.data = post.content_html
    form.featured_image.data = post.featured_image
    form.categories.data = [c.id for c in post.categories]


def _wp_stats():
    total_wp_posts = db.session.query(func.count(Post.id)).filter(Post.source == "wp").scalar() or 0
    localized_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp", Post.featured_image.like('/media/%')).scalar() or 0
    external_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp", Post.featured_image.isnot(None), ~Post.featured_image.like('/media/%')).scalar() or 0
    without_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp").filter((Post.featured_image.is_(None)) | (Post.featured_image == "")).scalar() or 0
    recent_posts = (Post.query.filter(Post.source == "wp")
                    .order_by(desc(Post.published_at), desc(Post.id))
                    .limit(30).all())
    return {
        "total_wp_posts": total_wp_posts,
        "localized_images": localized_images,
        "external_images": external_images,
        "without_images": without_images,
        "recent_posts": recent_posts,
    }


@admin_bp.app_context_processor
def inject_admin_helpers():
    return {"admin_media_root": current_app.config.get("MEDIA_ROOT", "/data/uploads")}


@admin_bp.get("/login")
def login():
    form = LoginForm()
    return render_template("admin/login.html", form=form)


@admin_bp.post("/login")
def login_post():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower().strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            return redirect(url_for("admin.dashboard"))
        flash("Email ou senha inválidos.", "danger")
    return render_template("admin/login.html", form=form)


@admin_bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("site.home"))


@admin_bp.get("/")
@login_required
def dashboard():
    r = _require_admin()
    if r:
        return r
    slots = AdSlot.query.order_by(AdSlot.key.asc()).all()
    media_files = []
    root = _media_root()
    if root.exists():
        for p in sorted([p for p in root.rglob("*") if p.is_file()], key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
            media_files.append({
                "name": p.name,
                "url": f"{current_app.config['MEDIA_URL_PREFIX'].rstrip('/')}/{p.relative_to(root).as_posix()}",
                "size_kb": max(1, round(p.stat().st_size / 1024)),
            })
    return render_template(
        "admin/dashboard.html",
        slots=slots,
        live_embed=_setting("live_embed_html", ""),
        logo_url=_setting("logo_url", ""),
        site_name=_setting("site_name", current_app.config.get("SITE_NAME", "News")),
        media_files=media_files,
        **_common_admin_context("dashboard"),
    )


@admin_bp.get("/insights")
@login_required
def insights_page():
    r = _require_admin()
    if r:
        return r

    end_default = datetime.utcnow().date()
    start_default = end_default - timedelta(days=29)
    start_day = _parse_date_input(request.args.get("from"), start_default)
    end_day = _parse_date_input(request.args.get("to"), end_default)
    insights = _analytics_stats(start_date=start_day, end_date=end_day)

    allowed_metrics = {
        "sessions": "Sessions",
        "pageviews": "Pageviews",
        "total_users": "Total Users",
    }
    selected_metric = (request.args.get("metric") or "sessions").strip().lower()
    if selected_metric not in allowed_metrics:
        selected_metric = "sessions"

    metric_chart = _build_chart_data(insights["daily_series"], selected_metric)

    return render_template(
        "admin/insights.html",
        insights=insights,
        selected_metric=selected_metric,
        metric_label=allowed_metrics[selected_metric],
        metric_chart=metric_chart,
        allowed_metrics=allowed_metrics,
        site_name=_setting("site_name", current_app.config.get("SITE_NAME", "News")),
        logo_url=_setting("logo_url", ""),
        favicon_url=_setting("favicon_url", ""),
        default_share_image=_setting("default_share_image", ""),
        default_meta_description=_setting("default_meta_description", ""),
        google_analytics_id=_setting("google_analytics_id", ""),
        facebook_app_id=_setting("facebook_app_id", ""),
        google_site_verification=_setting("google_site_verification", ""),
        site_keywords=_setting("site_keywords", ""),
        site_tagline=_setting("site_tagline", ""),
        contact_email=_setting("contact_email", ""),
        contact_phone=_setting("contact_phone", ""),
        instagram_url=_setting("instagram_url", ""),
        facebook_url=_setting("facebook_url", ""),
        youtube_url=_setting("youtube_url", ""),
        x_url=_setting("x_url", ""),
        **_common_admin_context("insights"),
    )


@admin_bp.get("/posts")
@login_required
def posts_list():
    r = _require_admin()
    if r:
        return r
    source = (request.args.get("source") or "all").strip()
    term = (request.args.get("q") or "").strip()
    q = Post.query
    if source in {"local", "wp"}:
        q = q.filter(Post.source == source)
    if term:
        like = f"%{term}%"
        q = q.filter(Post.title.ilike(like))
    posts = q.order_by(desc(Post.published_at), desc(Post.updated_at)).limit(200).all()
    return render_template("admin/posts_list.html", posts=posts, source=source, term=term, **_common_admin_context("posts"))


@admin_bp.route("/posts/new", methods=["GET", "POST"])
@login_required
def posts_new():
    r = _require_admin()
    if r:
        return r
    form = PostAdminForm()
    _bind_post_form_choices(form)
    if request.method == "POST" and form.validate_on_submit():
        action = (request.form.get("post_action") or "publish").strip().lower()
        is_draft = action == "draft"
        slug = _ensure_unique_slug(Post, form.title.data)
        featured_image = (form.featured_image.data or "").strip()
        if form.featured_image_file.data:
            featured_image = _save_upload(form.featured_image_file.data, "posts")
        now = datetime.utcnow()
        post = Post(
            source="local",
            title=form.title.data.strip(),
            slug=slug,
            excerpt=form.excerpt.data,
            content_html=form.content_html.data,
            featured_image=featured_image,
            author_name="Anônimo",
            is_draft=is_draft,
            published_at=None if is_draft else now,
            updated_at=now,
        )
        if form.categories.data:
            post.categories = Category.query.filter(Category.id.in_(form.categories.data)).all()
        db.session.add(post)
        db.session.commit()
        if not post.is_draft:
            _flash_hub_result('Publicação automática', _broadcast_post_to_hub(post) if _hub_config().get('auto_push') else {'sent': 0, 'ok': 0, 'results': []})
        flash('Rascunho salvo com sucesso.' if post.is_draft else 'Matéria publicada com sucesso.', 'success')
        return redirect(url_for("admin.posts_edit", post_id=post.id))
    return render_template("admin/post_form.html", form=form, mode="new", hub=_hub_config(), **_common_admin_context("posts"))


@admin_bp.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def posts_edit(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    form = PostAdminForm()
    _bind_post_form_choices(form)
    if request.method == "GET":
        _fill_post_form_from_obj(form, post)
    if request.method == "POST" and form.validate_on_submit():
        action = (request.form.get("post_action") or "publish").strip().lower()
        old_image = post.featured_image
        was_draft = bool(post.is_draft)
        post.title = form.title.data.strip()
        post.slug = _ensure_unique_slug(Post, form.title.data, object_id=post.id)
        post.excerpt = form.excerpt.data
        post.content_html = form.content_html.data
        post.author_name = "Anônimo"
        featured_image = (form.featured_image.data or "").strip()
        if form.featured_image_file.data:
            featured_image = _save_upload(form.featured_image_file.data, "posts")
        post.featured_image = featured_image
        post.is_draft = action == "draft"
        if post.is_draft:
            post.published_at = None
        elif post.published_at is None or was_draft:
            post.published_at = datetime.utcnow()
        post.updated_at = datetime.utcnow()
        post.categories = Category.query.filter(Category.id.in_(form.categories.data or [])).all()
        db.session.commit()
        if not post.is_draft:
            _flash_hub_result('Atualização automática', _broadcast_post_to_hub(post) if _hub_config().get('auto_push') else {'sent': 0, 'ok': 0, 'results': []})
        if form.featured_image_file.data and old_image and old_image != featured_image:
            _delete_local_media(old_image)
        flash('Rascunho atualizado.' if post.is_draft else 'Matéria publicada com sucesso.', 'success')
        return redirect(url_for("admin.posts_edit", post_id=post.id))
    return render_template("admin/post_form.html", form=form, mode="edit", post=post, hub=_hub_config(), **_common_admin_context("posts"))


@admin_bp.post("/posts/<int:post_id>/delete")
@login_required
def posts_delete(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    if post.source != "local":
        flash("Só é permitido excluir matérias locais pelo admin.", "warning")
        return redirect(url_for("admin.posts_list"))
    old_image = post.featured_image
    delete_snapshot = Post(title=post.title, slug=post.slug)
    db.session.delete(post)
    db.session.commit()
    _flash_hub_result('Remoção automática', _broadcast_delete_to_hub(delete_snapshot) if _hub_config().get('auto_push') else {'sent': 0, 'ok': 0, 'results': []})
    _delete_local_media(old_image)
    flash("Matéria removida.", "success")
    return redirect(url_for("admin.posts_list", source="local"))


@admin_bp.get("/categories")
@login_required
def categories_list():
    r = _require_admin()
    if r:
        return r
    categories = Category.query.order_by(Category.name.asc()).all()
    counts = {
        cid: total
        for cid, total in db.session.query(Category.id, func.count(post_categories.c.post_id))
        .outerjoin(post_categories, post_categories.c.category_id == Category.id)
        .group_by(Category.id)
        .all()
    }
    return render_template("admin/categories_list.html", categories=categories, counts=counts, **_common_admin_context("categories"))


@admin_bp.route("/categories/new", methods=["GET", "POST"])
@login_required
def categories_new():
    r = _require_admin()
    if r:
        return r
    form = CategoryForm()
    if request.method == "POST" and form.validate_on_submit():
        slug = _ensure_unique_slug(Category, form.slug.data or form.name.data)
        cat = Category(name=form.name.data.strip(), slug=slug)
        db.session.add(cat)
        db.session.commit()
        flash("Categoria criada.", "success")
        return redirect(url_for("admin.categories_list"))
    return render_template("admin/category_form.html", form=form, mode="new", **_common_admin_context("categories"))


@admin_bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@login_required
def categories_edit(category_id):
    r = _require_admin()
    if r:
        return r
    category = Category.query.get_or_404(category_id)
    form = CategoryForm(obj=category)
    if request.method == "POST" and form.validate_on_submit():
        category.name = form.name.data.strip()
        category.slug = _ensure_unique_slug(Category, form.slug.data or form.name.data, object_id=category.id)
        db.session.commit()
        flash("Categoria atualizada.", "success")
        return redirect(url_for("admin.categories_list"))
    return render_template("admin/category_form.html", form=form, mode="edit", category=category, **_common_admin_context("categories"))


@admin_bp.post("/categories/<int:category_id>/delete")
@login_required
def categories_delete(category_id):
    r = _require_admin()
    if r:
        return r
    category = Category.query.get_or_404(category_id)
    if Post.query.join(Post.categories).filter(Category.id == category.id).first():
        flash("Não foi possível excluir: existem matérias vinculadas a essa categoria.", "danger")
        return redirect(url_for("admin.categories_list"))
    db.session.delete(category)
    db.session.commit()
    flash("Categoria removida.", "success")
    return redirect(url_for("admin.categories_list"))


@admin_bp.get("/media")
@login_required
def media_library():
    r = _require_admin()
    if r:
        return r
    files = []
    root = _media_root()
    if root.exists():
        for p in sorted([p for p in root.rglob("*") if p.is_file()], key=lambda x: x.stat().st_mtime, reverse=True):
            rel = p.relative_to(root).as_posix()
            files.append({
                "name": p.name,
                "relative": rel,
                "url": f"{current_app.config['MEDIA_URL_PREFIX'].rstrip('/')}/{rel}",
                "size_kb": max(1, round(p.stat().st_size / 1024)),
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime),
            })
    return render_template("admin/media_library.html", files=files[:300], **_common_admin_context("media"))


@admin_bp.post("/media/upload")
@login_required
def media_upload():
    r = _require_admin()
    if r:
        return r
    files = request.files.getlist("files")
    saved = 0
    for f in files:
        if f and getattr(f, "filename", ""):
            _save_upload(f, "library")
            saved += 1
    flash(f"{saved} arquivo(s) enviado(s) para o volume.", "success")
    return redirect(url_for("admin.media_library"))


@admin_bp.post("/media/delete")
@login_required
def media_delete():
    r = _require_admin()
    if r:
        return r
    url = request.form.get("url", "")
    _delete_local_media(url)
    flash("Arquivo removido.", "success")
    return redirect(url_for("admin.media_library"))


@admin_bp.get("/settings")
@login_required
def settings_page():
    r = _require_admin()
    if r:
        return r
    return render_template(
        "admin/settings.html",
        live_embed=_setting("live_embed_html", ""),
        logo_url=_setting("logo_url", ""),
        favicon_url=_setting("favicon_url", ""),
        default_share_image=_setting("default_share_image", ""),
        site_name=_setting("site_name", current_app.config.get("SITE_NAME", "News")),
        site_tagline=_setting("site_tagline", ""),
        default_meta_description=_setting("default_meta_description", ""),
        facebook_app_id=_setting("facebook_app_id", ""),
        google_site_verification=_setting("google_site_verification", ""),
        google_analytics_id=_setting("google_analytics_id", ""),
        contact_email=_setting("contact_email", ""),
        contact_phone=_setting("contact_phone", ""),
        instagram_url=_setting("instagram_url", ""),
        facebook_url=_setting("facebook_url", ""),
        youtube_url=_setting("youtube_url", ""),
        x_url=_setting("x_url", ""),
        site_keywords=_setting("site_keywords", ""),
        **_common_admin_context("settings"),
    )


@admin_bp.post("/settings/live")
@login_required
def save_live():
    r = _require_admin()
    if r:
        return r
    _save_setting("live_embed_html", request.form.get("live_embed_html", ""))
    db.session.commit()
    flash("Bloco AO VIVO atualizado.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.post("/settings/logo")
@login_required
def save_logo():
    r = _require_admin()
    if r:
        return r
    old_logo = _setting("logo_url", "")
    old_favicon = _setting("favicon_url", "")
    old_share = _setting("default_share_image", "")
    logo_url = (request.form.get("logo_url", "") or "").strip()
    favicon_url = (request.form.get("favicon_url", "") or "").strip()
    default_share_image = (request.form.get("default_share_image", "") or "").strip()
    logo_file = request.files.get("logo_file")
    favicon_file = request.files.get("favicon_file")
    share_file = request.files.get("share_image_file")
    if logo_file and getattr(logo_file, "filename", ""):
        logo_url = _save_upload(logo_file, "branding")
    if favicon_file and getattr(favicon_file, "filename", ""):
        favicon_url = _save_upload(favicon_file, "branding")
    if share_file and getattr(share_file, "filename", ""):
        default_share_image = _save_upload(share_file, "branding")

    _save_setting("site_name", (request.form.get("site_name", "") or "").strip() or current_app.config.get("SITE_NAME", "News"))
    _save_setting("site_tagline", (request.form.get("site_tagline", "") or "").strip())
    _save_setting("default_meta_description", (request.form.get("default_meta_description", "") or "").strip())
    _save_setting("facebook_app_id", (request.form.get("facebook_app_id", "") or "").strip())
    _save_setting("google_site_verification", (request.form.get("google_site_verification", "") or "").strip())
    _save_setting("google_analytics_id", (request.form.get("google_analytics_id", "") or "").strip())
    _save_setting("contact_email", (request.form.get("contact_email", "") or "").strip())
    _save_setting("contact_phone", (request.form.get("contact_phone", "") or "").strip())
    _save_setting("instagram_url", (request.form.get("instagram_url", "") or "").strip())
    _save_setting("facebook_url", (request.form.get("facebook_url", "") or "").strip())
    _save_setting("youtube_url", (request.form.get("youtube_url", "") or "").strip())
    _save_setting("x_url", (request.form.get("x_url", "") or "").strip())
    _save_setting("site_keywords", (request.form.get("site_keywords", "") or "").strip())
    _save_setting("logo_url", logo_url)
    _save_setting("favicon_url", favicon_url)
    _save_setting("default_share_image", default_share_image)
    db.session.commit()
    if logo_file and old_logo and old_logo != logo_url:
        _delete_local_media(old_logo)
    if favicon_file and old_favicon and old_favicon != favicon_url:
        _delete_local_media(old_favicon)
    if share_file and old_share and old_share != default_share_image:
        _delete_local_media(old_share)
    flash("Configurações de branding, SEO e compartilhamento atualizadas.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.get("/ads/new")
@login_required
def ads_new():
    r = _require_admin()
    if r:
        return r
    form = AdSlotForm()
    form.is_active.data = True
    return render_template("admin/ad_form.html", form=form, mode="new", **_common_admin_context("ads"))


@admin_bp.post("/ads/new")
@login_required
def ads_new_post():
    r = _require_admin()
    if r:
        return r
    form = AdSlotForm()
    if form.validate_on_submit():
        if AdSlot.query.filter_by(key=form.key.data.strip()).first():
            flash("Já existe um slot com essa chave.", "danger")
            return render_template("admin/ad_form.html", form=form, mode="new", **_common_admin_context("ads"))
        html = form.html.data or ""
        img = (form.image_url.data or "").strip()
        if form.image_file.data:
            img = _save_upload(form.image_file.data, "ads")
        link = (form.link_url.data or "").strip() or "#"
        if img:
            html = f'<a href="{link}" target="_blank" rel="noopener"><img src="{img}" alt="" style="max-width:100%;height:auto;display:block;border-radius:10px;"></a>'
        slot = AdSlot(key=form.key.data.strip(), name=form.name.data.strip(), html=html, is_active=bool(form.is_active.data))
        db.session.add(slot)
        db.session.commit()
        flash("Slot criado.", "success")
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/ad_form.html", form=form, mode="new", **_common_admin_context("ads"))


@admin_bp.get("/ads/<int:slot_id>/edit")
@login_required
def ads_edit(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    form = AdSlotForm(obj=slot)
    return render_template("admin/ad_form.html", form=form, mode="edit", slot=slot, **_common_admin_context("ads"))


@admin_bp.post("/ads/<int:slot_id>/edit")
@login_required
def ads_edit_post(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    form = AdSlotForm()
    if form.validate_on_submit():
        slot.key = form.key.data.strip()
        slot.name = form.name.data.strip()
        html = form.html.data or ""
        img = (form.image_url.data or "").strip()
        if form.image_file.data:
            img = _save_upload(form.image_file.data, "ads")
        link = (form.link_url.data or "").strip() or "#"
        if img:
            html = f'<a href="{link}" target="_blank" rel="noopener"><img src="{img}" alt="" style="max-width:100%;height:auto;display:block;border-radius:10px;"></a>'
        slot.html = html
        slot.is_active = bool(form.is_active.data)
        db.session.commit()
        flash("Slot atualizado.", "success")
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/ad_form.html", form=form, mode="edit", slot=slot, **_common_admin_context("ads"))




@admin_bp.get("/hub-posts")
@login_required
def hub_posts_page():
    r = _require_admin()
    if r:
        return r
    hub = _hub_config()
    while len(hub['remotes']) < 3:
        hub['remotes'].append({'name': '', 'site_key': '', 'base_url': '', 'api_token': '', 'active': False})
    return render_template('admin/hub_posts.html', hub=hub, **_common_admin_context('hub_posts'))


@admin_bp.post('/hub-posts/save')
@login_required
def hub_posts_save():
    r = _require_admin()
    if r:
        return r
    _save_setting('hub_enabled', '1' if request.form.get('hub_enabled') else '0')
    _save_setting('hub_site_key', (request.form.get('hub_site_key') or '').strip())
    _save_setting('hub_receive_token', (request.form.get('hub_receive_token') or '').strip())
    _save_setting('hub_auto_push', '1' if request.form.get('hub_auto_push') else '0')
    _save_setting('hub_remote_sites_json', json.dumps(_parse_remote_sites_from_form(request.form), ensure_ascii=False))
    db.session.commit()
    flash('Hub Posts atualizado.', 'success')
    return redirect(url_for('admin.hub_posts_page'))


@admin_bp.post('/hub-posts/push/<int:post_id>')
@login_required
def hub_posts_push_single(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    result = _broadcast_post_to_hub(post)
    _flash_hub_result('Envio manual', result)
    return redirect(url_for('admin.posts_edit', post_id=post.id))


@admin_bp.post('/hub-posts/push-all')
@login_required
def hub_posts_push_all():
    r = _require_admin()
    if r:
        return r
    posts = Post.query.filter(Post.source.in_(['local', 'hub'])).order_by(desc(Post.published_at), desc(Post.id)).limit(200).all()
    total_sent = 0
    total_ok = 0
    for post in posts:
        result = _broadcast_post_to_hub(post)
        total_sent += int(result.get('sent', 0) or 0)
        total_ok += int(result.get('ok', 0) or 0)
    if total_sent:
        flash(f'Reenvio concluído: {total_ok}/{total_sent} sincronizações OK.', 'success' if total_ok == total_sent else 'warning')
    else:
        flash('Nenhum site remoto ativo configurado para reenviar.', 'warning')
    return redirect(url_for('admin.hub_posts_page'))


@admin_bp.get("/wordpress")
@login_required
def wordpress_manager():
    r = _require_admin()
    if r:
        return r
    stats = _wp_stats()
    return render_template("admin/wordpress.html", wp=stats, **_common_admin_context("wordpress"))


@admin_bp.post("/wordpress/sync")
@login_required
def wordpress_sync_page():
    r = _require_admin()
    if r:
        return r
    try:
        client = WPClient(current_app.config["WP_BASE_URL"])
        sync_categories(client)
        sync_posts(client, max_pages=50, per_page=current_app.config["WP_PER_PAGE"], download_images=True)
        flash("Importação do WordPress concluída com imagens salvas no servidor.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Erro na importação do WordPress: {e}", "danger")
    return redirect(url_for("admin.wordpress_manager"))


@admin_bp.post("/wordpress/localize-images")
@login_required
def wordpress_localize_images():
    r = _require_admin()
    if r:
        return r
    try:
        report = localize_existing_wp_images()
        flash(
            f"Imagens processadas. Posts atualizados: {report['updated_posts']}. Capa baixada: {report['featured_downloaded']}. Conteúdo atualizado: {report['content_updated']}.",
            "success",
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Erro ao baixar imagens pendentes: {e}", "danger")
    return redirect(url_for("admin.wordpress_manager"))


@admin_bp.post("/wordpress/clear")
@login_required
def wordpress_clear_posts():
    r = _require_admin()
    if r:
        return r
    removed = Post.query.filter(Post.source == "wp").delete(synchronize_session=False)
    db.session.commit()
    flash(f"Posts importados do WordPress removidos: {removed}.", "warning")
    return redirect(url_for("admin.wordpress_manager"))

@admin_bp.post("/sync/wp")
@login_required
def sync_wp_now():
    r = _require_admin()
    if r:
        return r
    try:
        client = WPClient(current_app.config["WP_BASE_URL"])
        sync_categories(client)
        sync_posts(client, max_pages=50, per_page=current_app.config["WP_PER_PAGE"], download_images=True)
        flash("Sincronização do WordPress concluída.", "success")
    except Exception as e:
        flash(f"Erro ao sincronizar: {e}", "danger")
    return redirect(url_for("admin.dashboard"))
