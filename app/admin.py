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
from sqlalchemy import func, desc, or_
from .storage import delete_media, is_managed_media_url, list_media_files, local_path_from_url, media_root, normalize_media_url, rewrite_media_urls, save_file_storage

from .models import db, User, AdSlot, SiteSetting, PageView, Post, Category, post_categories, AnalyticsSession
from .sync import download_external_image
from .news_api import search_google_news, generate_article_with_openai, enrich_news_item, test_openai_connection
from .forms import LoginForm, AdSlotForm, CategoryForm, PostAdminForm
from .social_whatsapp import (
    DEFAULT_CAPTION_TEMPLATE,
    auto_send_post_to_whatsapp,
    get_whatsapp_groups,
    get_whatsapp_status,
    mark_post_sent,
    save_whatsapp_settings,
    send_post_to_whatsapp,
    send_whatsapp_test_message,
    whatsapp_settings,
)
from .wp_client import WPClient
from .sync import sync_categories, sync_posts, localize_existing_wp_images
from html import unescape, escape

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


def _selected_top_menu_category_ids() -> list[int]:
    raw = _setting_json('top_menu_category_ids', [])
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        try:
            result.append(int(item))
        except Exception:
            continue
    return result


def _parse_ad_slot_payload(raw: str | None) -> dict | None:
    value = (raw or '').strip()
    if not value.startswith('__ADCFG__'):
        return None
    try:
        payload = json.loads(value[len('__ADCFG__'):])
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _default_slot_layout_meta() -> dict:
    return {
        'header_top': {
            'label': 'Topo do site',
            'hint': 'Faixa principal do cabeçalho',
            'shape': 'wide',
            'dimensions': '1170 × 250 px',
        },
        'home_top': {
            'label': 'Meio da home',
            'hint': 'Banner exibido no meio da página inicial',
            'shape': 'wide',
            'dimensions': '1170 × 250 px',
        },
        'home_mid': {
            'label': 'Final da matéria',
            'hint': 'Banner exibido somente no fim das matérias',
            'shape': 'wide',
            'dimensions': '1170 × 250 px',
        },
        'home_bottom': {
            'label': 'Rodapé',
            'hint': 'Banner grande no rodapé do site',
            'shape': 'wide',
            'dimensions': '420 × 170 px',
        },
        'sidebar_1': {
            'label': 'Lateral 1',
            'hint': 'Primeiro banner lateral da sessão de categorias',
            'shape': 'square',
            'dimensions': '300 × 300 px',
        },
        'sidebar_2': {
            'label': 'Lateral 2',
            'hint': 'Segundo banner lateral da sessão de categorias',
            'shape': 'square',
            'dimensions': '300 × 300 px',
        },
    }


def _slot_visual_payload(slot: AdSlot) -> dict:
    payload = _parse_ad_slot_payload(slot.html) or {}
    banners = payload.get('banners') if isinstance(payload.get('banners'), list) else []
    clean_banners = []
    for item in banners:
        if not isinstance(item, dict):
            continue
        image = (item.get('image') or '').strip()
        if not image:
            continue
        clean_banners.append({
            'image': image,
            'link': (item.get('link') or '').strip(),
            'title': (item.get('title') or '').strip(),
        })
    interval_seconds = payload.get('interval_seconds', 5)
    try:
        interval_seconds = max(1, min(int(interval_seconds), 120))
    except Exception:
        interval_seconds = 5
    return {
        'mode': 'visual' if payload else ('html' if (slot.html or '').strip() else 'empty'),
        'interval_seconds': interval_seconds,
        'banners': clean_banners,
        'raw_html': '' if payload else (slot.html or ''),
    }


def _slot_card_data(slot: AdSlot) -> dict:
    meta = _default_slot_layout_meta().get(slot.key, {})
    payload = _slot_visual_payload(slot)
    return {
        'slot': slot,
        'label': meta.get('label', slot.name),
        'hint': meta.get('hint', 'Gerencie as artes desse espaço.'),
        'shape': meta.get('shape', 'wide'),
        'dimensions': meta.get('dimensions', 'Consulte a arte usada no site'),
        'interval_seconds': payload['interval_seconds'],
        'banner_count': len(payload['banners']),
        'cover_image': payload['banners'][0]['image'] if payload['banners'] else '',
        'mode': payload['mode'],
    }


def _build_slot_payload_from_request(slot: AdSlot):
    titles = request.form.getlist('banner_title[]')
    links = request.form.getlist('banner_link[]')
    existing_images = request.form.getlist('banner_existing_image[]')
    uploads = request.files.getlist('banner_image[]')
    total = max(len(titles), len(links), len(existing_images), len(uploads))
    banners = []
    old_local_images = set()
    existing_payload = _slot_visual_payload(slot)
    for item in existing_payload.get('banners', []):
        image = (item.get('image') or '').strip()
        if is_managed_media_url(image):
            old_local_images.add(normalize_media_url(image))

    kept_local_images = set()
    for idx in range(total):
        title = (titles[idx] if idx < len(titles) else '').strip()
        link = (links[idx] if idx < len(links) else '').strip()
        existing_image = (existing_images[idx] if idx < len(existing_images) else '').strip()
        upload = uploads[idx] if idx < len(uploads) else None
        image = existing_image
        if upload and getattr(upload, 'filename', ''):
            image = _save_upload(upload, 'ads')
        if not image:
            continue
        if is_managed_media_url(image):
            kept_local_images.add(normalize_media_url(image))
        banners.append({'title': title, 'link': link, 'image': image})

    interval_raw = (request.form.get('interval_seconds') or '5').strip()
    try:
        interval_seconds = max(1, min(int(interval_raw), 120))
    except Exception:
        interval_seconds = 5

    payload = {
        'version': 1,
        'name': slot.name,
        'interval_seconds': interval_seconds,
        'banners': banners,
    }

    removed_local_images = sorted(old_local_images - kept_local_images)
    return payload, removed_local_images


def _absolute_media_url(value: str) -> str:
    value = normalize_media_url(value or "")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    base = request.url_root.rstrip("/")
    return f"{base}{value if value.startswith('/') else '/' + value}"




def _normalize_post_media(post: Post | None):
    if not post:
        return post
    if getattr(post, "featured_image", None):
        post.featured_image = normalize_media_url(post.featured_image)
    if getattr(post, "content_html", None):
        post.content_html = rewrite_media_urls(post.content_html)
    return post


def _normalize_posts_media(posts):
    return [_normalize_post_media(post) for post in (posts or [])]

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
            'author_name': post.author_name or 'Redação',
            'published_at': post.published_at.isoformat() if post.published_at else '',
            'updated_at': (post.updated_at or datetime.utcnow()).isoformat(),
            'categories': [{'name': c.name, 'slug': c.slug} for c in (post.categories or [])],
            'source': post.source or 'local',
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
    return media_root()


def _save_upload(file_storage, subdir: str = "general") -> str:
    return save_file_storage(file_storage, subdir=subdir)


def _local_media_path_from_url(url: str) -> Path | None:
    return local_path_from_url(url)


def _delete_local_media(url: str) -> None:
    delete_media(url)



def _parse_date_input(value: str | None, fallback: date) -> date:
    raw = (value or '').strip()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return fallback

def _post_count_for_day(day: date) -> int:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    return Post.query.filter(Post.published_at >= start, Post.published_at < end).count()


def _posts_for_day(day: date, limit: int = 12):
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    return _normalize_posts_media(
        Post.query.filter(Post.published_at >= start, Post.published_at < end)
        .order_by(desc(Post.published_at), desc(Post.id))
        .limit(limit)
        .all()
    )


def _posts_until_day(day: date, limit: int = 16):
    end = datetime.combine(day, time.max)
    return _normalize_posts_media(
        Post.query.filter(Post.published_at.isnot(None), Post.published_at <= end)
        .order_by(desc(Post.published_at), desc(Post.id))
        .limit(limit)
        .all()
    )


def _post_count_until_day(day: date) -> int:
    end = datetime.combine(day, time.max)
    return Post.query.filter(Post.published_at.isnot(None), Post.published_at <= end).count()


def _home_calendar_days_summary(days: int = 14):
    today = datetime.utcnow().date()
    items = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        items.append({
            'date': day,
            'iso': day.isoformat(),
            'label': day.strftime('%d/%m/%Y'),
            'count': _post_count_for_day(day),
        })
    return items


def _build_chart_data(daily_series, metric_key: str):
    width = 980
    height = 280
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
        "recent_posts": _normalize_posts_media(recent_posts),
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
    form.featured_image.data = normalize_media_url(post.featured_image or "")
    form.categories.data = [c.id for c in post.categories]
    form.scheduled_for.data = post.published_at


def _wp_stats():
    total_wp_posts = db.session.query(func.count(Post.id)).filter(Post.source == "wp").scalar() or 0
    localized_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp", or_(Post.featured_image.like('/media/%'), Post.featured_image.like('https://img.%'))).scalar() or 0
    external_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp", Post.featured_image.isnot(None), ~Post.featured_image.like('/media/%'), ~Post.featured_image.like('https://img.%')).scalar() or 0
    without_images = db.session.query(func.count(Post.id)).filter(Post.source == "wp").filter((Post.featured_image.is_(None)) | (Post.featured_image == "")).scalar() or 0
    recent_posts = (Post.query.filter(Post.source == "wp")
                    .order_by(desc(Post.published_at), desc(Post.id))
                    .limit(30).all())
    return {
        "total_wp_posts": total_wp_posts,
        "localized_images": localized_images,
        "external_images": external_images,
        "without_images": without_images,
        "recent_posts": _normalize_posts_media(recent_posts),
    }


@admin_bp.app_context_processor
def inject_admin_helpers():
    return {
        "admin_media_root": current_app.config.get("MEDIA_ROOT", "/data/uploads"),
        "format_date_br": lambda value: value.strftime("%d/%m/%Y %H:%M") if value else "",
        "media_url": normalize_media_url,
    }


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
            if not getattr(user, 'is_active', True):
                flash('Este usuário está desativado. Fale com o administrador.', 'danger')
                return render_template("admin/login.html", form=form)
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
    media_files = [
        {"name": item.name, "url": item.url, "size_kb": item.size_kb}
        for item in list_media_files(limit=8)
    ]
    return render_template(
        "admin/dashboard.html",
        slots=slots,
        live_embed=_setting("live_embed_html", ""),
        logo_url=normalize_media_url(_setting("logo_url", "")),
        site_name=_setting("site_name", current_app.config.get("SITE_NAME", "News")),
        media_files=media_files,
        **_common_admin_context("dashboard"),
    )



@admin_bp.route("/calendario-home", methods=["GET", "POST"])
@login_required
def home_calendar_page():
    r = _require_admin()
    if r:
        return r

    today = datetime.utcnow().date()
    current_date = _parse_date_input(_setting("home_calendar_date", ""), today)

    if request.method == "POST":
        selected_date = _parse_date_input(request.form.get("home_calendar_date"), today)
        is_enabled = bool(request.form.get("home_calendar_enabled"))
        _save_setting("home_calendar_enabled", "1" if is_enabled else "0")
        _save_setting("home_calendar_date", selected_date.isoformat())
        db.session.commit()
        status = "ativado" if is_enabled else "desativado"
        flash(f"Calendário Home {status}. A home exibirá matérias de {selected_date.strftime('%d/%m/%Y')} para trás.", "success")
        return redirect(url_for("admin.home_calendar_page"))

    enabled = _setting_bool("home_calendar_enabled", False)
    selected_posts = _posts_until_day(current_date, limit=16)
    post_count = _post_count_for_day(current_date)
    total_until_count = _post_count_until_day(current_date)
    return render_template(
        "admin/home_calendar.html",
        enabled=enabled,
        selected_date=current_date,
        selected_date_iso=current_date.isoformat(),
        selected_date_label=current_date.strftime('%d/%m/%Y'),
        selected_posts=selected_posts,
        post_count=post_count,
        total_until_count=total_until_count,
        days_summary=_home_calendar_days_summary(14),
        home_url=url_for("site.home", _external=True),
        **_common_admin_context("home_calendar"),
    )


@admin_bp.post("/materias-api/test-openai")
@login_required
def materias_api_test_openai():
    r = _require_admin()
    if r:
        return r
    status = test_openai_connection()
    flash(status.get("message") or ("GPT conectado." if status.get("ok") else "GPT indisponível."), "success" if status.get("ok") else "danger")
    return redirect(url_for("admin.materias_api_page"))


@admin_bp.route("/materias-api", methods=["GET", "POST"])
@login_required
def materias_api_page():
    r = _require_admin()
    if r:
        return r

    categories = Category.query.order_by(Category.name.asc()).all()
    results = []
    searched = False
    query = (request.values.get("q") or "").strip()
    selected_date = (request.values.get("data") or "").strip()

    if request.method == "POST" and (request.form.get("materias_action") or "") == "create_posts":
        raw_items = request.form.get("items_json") or "[]"
        try:
            available_items = json.loads(raw_items)
            if not isinstance(available_items, list):
                available_items = []
        except Exception:
            available_items = []

        selected_indexes = set()
        for value in request.form.getlist("selected_items"):
            try:
                selected_indexes.add(int(value))
            except Exception:
                continue

        if not selected_indexes:
            flash("Selecione pelo menos uma matéria para gerar.", "warning")
            results = available_items
            searched = True
        else:
            publish_now = (request.form.get("post_action") or "draft") == "publish"
            category_id_raw = (request.form.get("category_id") or "").strip()
            category = None
            if category_id_raw:
                try:
                    category = Category.query.get(int(category_id_raw))
                except Exception:
                    category = None
            if not category:
                category = Category.query.filter(func.lower(Category.name) == "notícias").first()
                if not category:
                    category = Category(name="Notícias", slug=_ensure_unique_slug(Category, "noticias"))
                    db.session.add(category)
                    db.session.flush()

            created = []
            errors = []
            site_tone = _setting("materias_api_tom", "Portal regional brasileiro, linguagem jornalística clara, objetiva, popular e informativa.")
            now = datetime.utcnow()
            for idx in sorted(selected_indexes):
                if idx < 0 or idx >= len(available_items):
                    continue
                item = available_items[idx]
                try:
                    item = enrich_news_item(item, include_text=True)
                    article = generate_article_with_openai(item, site_tone=site_tone)
                    title = (article.get("titulo") or item.get("title") or "Matéria").strip()
                    content_html = (article.get("corpo_html") or "").strip()
                    excerpt = (article.get("resumo") or article.get("subtitulo") or item.get("description") or "").strip()
                    source_name = (article.get("fonte_nome") or item.get("source") or "Fonte original").strip()
                    source_url = (article.get("fonte_url") or item.get("url") or "").strip()
                    if source_url and source_url not in content_html:
                        content_html += f'<p><strong>Fonte:</strong> <a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener nofollow">{escape(source_name)}</a>.</p>'

                    featured_image = (item.get("image") or "").strip()
                    image_note = ""
                    if featured_image:
                        try:
                            downloaded_image = download_external_image(featured_image, folder="materias-api/featured")
                            if downloaded_image:
                                featured_image = downloaded_image
                                image_note = "imagem salva"
                            else:
                                image_note = "imagem não baixada"
                        except Exception as img_exc:
                            image_note = f"imagem não baixada: {str(img_exc)[:80]}"
                            featured_image = ""

                    if article.get("erro_ia"):
                        content_html += f'<p><em>Observação interna: GPT não respondeu e foi usado rascunho básico. Erro: {escape(str(article.get("erro_ia"))[:180])}</em></p>'

                    post = Post(
                        source="local",
                        title=title,
                        slug=_ensure_unique_slug(Post, title or "materia-api"),
                        excerpt=excerpt[:3000],
                        content_html=content_html,
                        featured_image=featured_image,
                        author_name="Redação",
                        updated_at=now,
                        published_at=now if publish_now else None,
                    )
                    post.categories = [category]
                    db.session.add(post)
                    db.session.flush()
                    created.append(post)
                except Exception as exc:
                    errors.append(str(exc)[:180])

            db.session.commit()
            whatsapp_sent = 0
            whatsapp_errors = []
            for post in created:
                if post.published_at and post.published_at <= datetime.utcnow() and _hub_config().get('enabled'):
                    _broadcast_post_to_hub(post)
                if post.published_at and post.published_at <= datetime.utcnow():
                    wa_result = auto_send_post_to_whatsapp(post)
                    if wa_result.ok and whatsapp_settings().get('enabled') and whatsapp_settings().get('auto_send'):
                        whatsapp_sent += 1
                    elif not wa_result.ok:
                        whatsapp_errors.append(wa_result.message)
            if created:
                extra = f" WhatsApp: {whatsapp_sent} envio(s)." if whatsapp_sent else ""
                flash(f"{len(created)} matéria(s) criada(s) com sucesso como {'publicadas' if publish_now else 'rascunho'}.{extra}", "success")
            if whatsapp_errors:
                flash("Alguns envios para WhatsApp falharam: " + " | ".join(whatsapp_errors[:3]), "warning")
            if errors:
                flash("Algumas matérias não foram criadas: " + " | ".join(errors[:3]), "warning")
            return redirect(url_for("admin.posts_list", source="local"))

    elif query:
        searched = True
        try:
            limit = min(max(int(request.values.get("limit") or 15), 1), 30)
        except Exception:
            limit = 15
        try:
            results = search_google_news(query, day=selected_date or None, limit=limit, enrich_images=True)
            if not results:
                flash("Nenhum resultado encontrado para essa busca.", "warning")
        except Exception as exc:
            flash(f"Erro ao buscar notícias: {str(exc)[:180]}", "danger")
            results = []

    return render_template(
        "admin/materias_api.html",
        query=query,
        selected_date=selected_date,
        results=results,
        searched=searched,
        categories=categories,
        openai_enabled=bool((os.getenv("OPENAI_API_KEY") or "").strip()),
        **_common_admin_context("materias_api"),
    )


@admin_bp.get("/posts")
@login_required
def posts_list():
    r = _require_admin()
    if r:
        return r
    term = (request.args.get("q") or "").strip()
    source = (request.args.get("source") or "all").strip().lower()
    q = Post.query
    if term:
        q = q.filter(Post.title.ilike(f"%{term}%"))
    if source in {"local", "wp", "hub"}:
        q = q.filter(Post.source == source)
    else:
        source = "all"
    posts = q.order_by(desc(Post.published_at), desc(Post.updated_at), desc(Post.id)).all()
    return render_template(
        "admin/posts_list.html",
        posts=_normalize_posts_media(posts),
        term=term,
        source=source,
        now_utc=datetime.utcnow(),
        format_date_br=lambda value: value.strftime("%d/%m/%Y %H:%M") if value else "",
        **_common_admin_context("posts"),
    )


@admin_bp.route("/posts/new", methods=["GET", "POST"])
@login_required
def posts_new():
    r = _require_admin()
    if r:
        return r
    form = PostAdminForm()
    _bind_post_form_choices(form)
    if request.method == "POST" and form.validate_on_submit():
        featured_image = (form.featured_image.data or "").strip()
        if form.featured_image_file.data:
            featured_image = _save_upload(form.featured_image_file.data, "posts")
        now = datetime.utcnow()
        scheduled_for = form.scheduled_for.data
        post = Post(
            source="local",
            title=(form.title.data or "").strip(),
            slug=_ensure_unique_slug(Post, form.title.data or "materia"),
            excerpt=(form.excerpt.data or "").strip(),
            content_html=(form.content_html.data or "").strip(),
            featured_image=featured_image,
            author_name="Redação",
            updated_at=now,
        )
        if (request.form.get("post_action") or "publish") == "publish":
            post.published_at = scheduled_for or now
        post.categories = Category.query.filter(Category.id.in_(form.categories.data or [])).all()
        db.session.add(post)
        db.session.commit()
        if post.published_at and post.published_at <= datetime.utcnow() and _hub_config().get('enabled'):
            _broadcast_post_to_hub(post)
        if post.published_at and post.published_at <= datetime.utcnow():
            wa_result = auto_send_post_to_whatsapp(post)
            if not wa_result.ok:
                flash(f"Matéria criada, mas o envio para WhatsApp falhou: {wa_result.message}", "warning")
            elif whatsapp_settings().get('enabled') and whatsapp_settings().get('auto_send'):
                flash("Matéria criada e enviada para o WhatsApp.", "success")
                return redirect(url_for("admin.posts_edit", post_id=post.id))
        flash("Matéria criada com sucesso.", "success")
        return redirect(url_for("admin.posts_edit", post_id=post.id))
    return render_template("admin/post_form.html", form=form, mode="new", post=None, hub=_hub_config(), now_utc=datetime.utcnow(), **_common_admin_context("posts"))


@admin_bp.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def posts_edit(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    was_published = bool(post.published_at and post.published_at <= datetime.utcnow())
    form = PostAdminForm(obj=post)
    _bind_post_form_choices(form)
    if request.method == "GET":
        _fill_post_form_from_obj(form, post)
    elif form.validate_on_submit():
        featured_image = (form.featured_image.data or "").strip()
        old_featured_image = post.featured_image or ""
        if form.featured_image_file.data:
            featured_image = _save_upload(form.featured_image_file.data, "posts")
        post.title = (form.title.data or "").strip()
        post.slug = _ensure_unique_slug(Post, post.title or post.slug or "materia", object_id=post.id)
        post.excerpt = (form.excerpt.data or "").strip()
        post.content_html = (form.content_html.data or "").strip()
        post.featured_image = featured_image
        post.author_name = "Redação"
        post.updated_at = datetime.utcnow()
        action = (request.form.get("post_action") or "publish")
        scheduled_for = form.scheduled_for.data
        if action == "publish":
            post.published_at = scheduled_for or post.published_at or datetime.utcnow()
        else:
            post.published_at = None
        post.categories = Category.query.filter(Category.id.in_(form.categories.data or [])).all()
        db.session.commit()
        if form.featured_image_file.data and old_featured_image and old_featured_image != featured_image:
            _delete_local_media(old_featured_image)
        if post.published_at and post.published_at <= datetime.utcnow() and _hub_config().get('enabled'):
            _broadcast_post_to_hub(post)
        is_published_now = bool(post.published_at and post.published_at <= datetime.utcnow())
        if is_published_now and not was_published:
            wa_result = auto_send_post_to_whatsapp(post)
            if not wa_result.ok:
                flash(f"Matéria atualizada, mas o envio para WhatsApp falhou: {wa_result.message}", "warning")
            elif whatsapp_settings().get('enabled') and whatsapp_settings().get('auto_send'):
                flash("Matéria publicada e enviada para o WhatsApp.", "success")
                return redirect(url_for("admin.posts_edit", post_id=post.id))
        flash("Matéria atualizada com sucesso.", "success")
        return redirect(url_for("admin.posts_edit", post_id=post.id))
    return render_template("admin/post_form.html", form=form, mode="edit", post=_normalize_post_media(post), hub=_hub_config(), now_utc=datetime.utcnow(), **_common_admin_context("posts"))


@admin_bp.post("/posts/<int:post_id>/delete")
@login_required
def posts_delete(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    if post.source != 'local':
        flash('Somente matérias locais podem ser excluídas por aqui.', 'warning')
        return redirect(url_for('admin.posts_edit', post_id=post.id))
    featured_image = post.featured_image or ''
    post.categories = []
    PageView.query.filter(PageView.post_id == post.id).update({PageView.post_id: None}, synchronize_session=False)
    db.session.delete(post)
    db.session.commit()
    if is_managed_media_url(featured_image):
        _delete_local_media(featured_image)
    flash('Matéria excluída com sucesso.', 'success')
    return redirect(url_for('admin.posts_list'))


@admin_bp.get("/categories")
@login_required
def categories_list():
    r = _require_admin()
    if r:
        return r
    categories = Category.query.order_by(Category.name.asc()).all()
    rows = (
        db.session.query(Category.id, func.count(post_categories.c.post_id))
        .outerjoin(post_categories, post_categories.c.category_id == Category.id)
        .group_by(Category.id)
        .all()
    )
    counts = {row[0]: row[1] for row in rows}
    return render_template("admin/categories_list.html", categories=categories, counts=counts, **_common_admin_context("categories"))


@admin_bp.route("/categories/new", methods=["GET", "POST"])
@login_required
def categories_new():
    r = _require_admin()
    if r:
        return r
    form = CategoryForm()
    if form.validate_on_submit():
        desired_slug = (form.slug.data or form.name.data or "").strip()
        category = Category(name=(form.name.data or "").strip(), slug=_ensure_unique_slug(Category, desired_slug))
        db.session.add(category)
        db.session.commit()
        flash("Categoria criada com sucesso.", "success")
        return redirect(url_for("admin.categories_edit", category_id=category.id))
    return render_template("admin/category_form.html", form=form, mode="new", category=None, **_common_admin_context("categories"))


@admin_bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@login_required
def categories_edit(category_id):
    r = _require_admin()
    if r:
        return r
    category = Category.query.get_or_404(category_id)
    form = CategoryForm(obj=category)
    if form.validate_on_submit():
        category.name = (form.name.data or "").strip()
        desired_slug = (form.slug.data or category.name or "").strip()
        category.slug = _ensure_unique_slug(Category, desired_slug, object_id=category.id)
        db.session.commit()
        flash("Categoria atualizada com sucesso.", "success")
        return redirect(url_for("admin.categories_edit", category_id=category.id))
    return render_template("admin/category_form.html", form=form, mode="edit", category=category, **_common_admin_context("categories"))


@admin_bp.post("/categories/<int:category_id>/delete")
@login_required
def categories_delete(category_id):
    r = _require_admin()
    if r:
        return r
    category = Category.query.get_or_404(category_id)
    in_use = db.session.query(func.count(post_categories.c.post_id)).filter(post_categories.c.category_id == category.id).scalar() or 0
    if in_use:
        flash("Essa categoria está vinculada a matérias e não pode ser excluída agora.", "warning")
        return redirect(url_for("admin.categories_edit", category_id=category.id))
    db.session.delete(category)
    db.session.commit()
    flash("Categoria excluída com sucesso.", "success")
    return redirect(url_for("admin.categories_list"))


@admin_bp.get("/media")
@login_required
def media_library():
    r = _require_admin()
    if r:
        return r
    files = [
        {'name': item.name, 'url': item.url, 'size_kb': item.size_kb, 'updated_at': item.updated_at}
        for item in list_media_files()
    ]
    return render_template("admin/media_library.html", files=files, **_common_admin_context("media"))


@admin_bp.post("/media/upload")
@login_required
def media_upload():
    r = _require_admin()
    if r:
        return r
    uploads = request.files.getlist('files')
    sent = 0
    for item in uploads:
        if item and getattr(item, 'filename', ''):
            _save_upload(item, 'library')
            sent += 1
    flash(f'{sent} arquivo(s) enviado(s) com sucesso.' if sent else 'Selecione pelo menos um arquivo para enviar.', 'success' if sent else 'warning')
    return redirect(url_for('admin.media_library'))


@admin_bp.post("/media/delete")
@login_required
def media_delete():
    r = _require_admin()
    if r:
        return r
    url = (request.form.get('url') or '').strip()
    if url:
        _delete_local_media(url)
        flash('Arquivo removido com sucesso.', 'success')
    else:
        flash('Arquivo inválido.', 'warning')
    return redirect(url_for('admin.media_library'))


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

    visible_card_keys = {"sessions", "pageviews", "total_users"}
    visible_cards = [card for card in insights["cards"] if card["key"] in visible_card_keys]
    dashboard_stats = _dashboard_stats()
    visible_cards.append({
        "key": "pageviews_24h",
        "label": "Pageviews totais",
        "value": (dashboard_stats.get("pv_total") or 0),
        "delta": 0,
        "is_total": True,
        "context_value": dashboard_stats.get("pv_24h") or 0,
    })

    return render_template(
        "admin/insights.html",
        insights=insights,
        visible_cards=visible_cards,
        selected_metric=selected_metric,
        metric_label=allowed_metrics[selected_metric],
        metric_chart=metric_chart,
        allowed_metrics=allowed_metrics,
        site_name=_setting("site_name", current_app.config.get("SITE_NAME", "News")),
        logo_url=normalize_media_url(_setting("logo_url", "")),
        favicon_url=normalize_media_url(_setting("favicon_url", "")),
        default_share_image=normalize_media_url(_setting("default_share_image", "")),
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




@admin_bp.get("/users")
@login_required
def users_list():
    r = _require_admin()
    if r:
        return r
    term = (request.args.get('q') or '').strip()
    q = User.query
    if term:
        q = q.filter(User.email.ilike(f"%{term}%"))
    users = q.order_by(User.is_admin.desc(), User.email.asc()).all()
    return render_template(
        'admin/users_list.html',
        users=users,
        term=term,
        top_menu_category_ids=_selected_top_menu_category_ids(),
        **_common_admin_context('users'),
    )


@admin_bp.route('/users/new', methods=['GET', 'POST'])
@login_required
def users_new():
    r = _require_admin()
    if r:
        return r
    user_obj = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        is_admin = bool(request.form.get('is_admin'))
        is_active = bool(request.form.get('is_active'))

        if not email:
            flash('Informe o e-mail do usuário.', 'danger')
        elif len(password) < 4:
            flash('A senha precisa ter pelo menos 4 caracteres.', 'danger')
        elif User.query.filter_by(email=email).first():
            flash('Já existe um usuário com esse e-mail.', 'danger')
        else:
            user_obj = User(email=email, is_admin=is_admin, is_active=is_active)
            user_obj.set_password(password)
            db.session.add(user_obj)
            db.session.commit()
            flash('Usuário criado com sucesso.', 'success')
            return redirect(url_for('admin.users_edit', user_id=user_obj.id))

    return render_template('admin/user_form.html', mode='new', user_obj=user_obj, **_common_admin_context('users'))


@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def users_edit(user_id):
    r = _require_admin()
    if r:
        return r
    user_obj = User.query.get_or_404(user_id)
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        is_admin = bool(request.form.get('is_admin'))
        is_active = bool(request.form.get('is_active'))

        if not email:
            flash('Informe o e-mail do usuário.', 'danger')
        elif User.query.filter(User.email == email, User.id != user_obj.id).first():
            flash('Já existe outro usuário com esse e-mail.', 'danger')
        elif user_obj.id == current_user.id and not is_admin:
            flash('Você não pode remover seu próprio acesso de administrador.', 'danger')
        elif user_obj.id == current_user.id and not is_active:
            flash('Você não pode desativar seu próprio usuário.', 'danger')
        else:
            user_obj.email = email
            user_obj.is_admin = is_admin
            user_obj.is_active = is_active
            if password.strip():
                if len(password.strip()) < 4:
                    flash('A nova senha precisa ter pelo menos 4 caracteres.', 'danger')
                    return render_template('admin/user_form.html', mode='edit', user_obj=user_obj, **_common_admin_context('users'))
                user_obj.set_password(password.strip())
            db.session.commit()
            flash('Usuário atualizado com sucesso.', 'success')
            return redirect(url_for('admin.users_edit', user_id=user_obj.id))

    return render_template('admin/user_form.html', mode='edit', user_obj=user_obj, **_common_admin_context('users'))


@admin_bp.post('/users/<int:user_id>/toggle-active')
@login_required
def users_toggle_active(user_id):
    r = _require_admin()
    if r:
        return r
    user_obj = User.query.get_or_404(user_id)
    if user_obj.id == current_user.id:
        flash('Você não pode desativar seu próprio usuário.', 'danger')
        return redirect(url_for('admin.users_list'))
    user_obj.is_active = not bool(user_obj.is_active)
    db.session.commit()
    flash('Usuário ativado com sucesso.' if user_obj.is_active else 'Usuário desativado com sucesso.', 'success')
    return redirect(url_for('admin.users_list'))


@admin_bp.post('/users/<int:user_id>/toggle-admin')
@login_required
def users_toggle_admin(user_id):
    r = _require_admin()
    if r:
        return r
    user_obj = User.query.get_or_404(user_id)
    if user_obj.id == current_user.id and user_obj.is_admin:
        flash('Você não pode remover seu próprio acesso de administrador.', 'danger')
        return redirect(url_for('admin.users_list'))
    user_obj.is_admin = not bool(user_obj.is_admin)
    db.session.commit()
    flash('Permissão de administrador atualizada.', 'success')
    return redirect(url_for('admin.users_list'))


def _only_digits(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _normalize_brazil_phone(value: str | None) -> str:
    digits = _only_digits(value)
    if not digits:
        return ""
    if digits.startswith("55"):
        return digits
    if len(digits) in {10, 11}:
        return f"55{digits}"
    return digits


def _is_foz_location(candidate: dict) -> bool:
    joined = ' '.join([
        candidate.get('formatted_address', '') or '',
        candidate.get('city', '') or '',
        candidate.get('state', '') or '',
        candidate.get('country', '') or '',
    ]).lower()
    return 'foz do iguacu' in joined or 'foz do iguaçu' in joined


def _build_maps_route_url(address: str = '', latitude: str = '', longitude: str = '') -> str:
    if latitude and longitude:
        return f"https://www.google.com/maps/dir/?api=1&destination={latitude},{longitude}"
    if address:
        return f"https://www.google.com/maps/dir/?api=1&destination={requests.utils.quote(address)}"
    return ''


def _extract_place_address_components(components: list[dict] | None) -> dict:
    data = {'city': '', 'state': '', 'country': '', 'postal_code': '', 'neighborhood': ''}
    for item in components or []:
        types = item.get('types') or []
        long_name = (item.get('long_name') or '').strip()
        short_name = (item.get('short_name') or '').strip()
        if 'administrative_area_level_2' in types and not data['city']:
            data['city'] = long_name
        elif 'locality' in types and not data['city']:
            data['city'] = long_name
        elif 'administrative_area_level_1' in types and not data['state']:
            data['state'] = short_name or long_name
        elif 'country' in types and not data['country']:
            data['country'] = long_name
        elif 'postal_code' in types and not data['postal_code']:
            data['postal_code'] = long_name
        elif ('sublocality' in types or 'sublocality_level_1' in types or 'neighborhood' in types) and not data['neighborhood']:
            data['neighborhood'] = long_name
    return data



@admin_bp.get("/whatsapp")
@login_required
def whatsapp_page():
    r = _require_admin()
    if r:
        return r
    cfg = whatsapp_settings()
    status = get_whatsapp_status() if cfg.get("service_url") else None
    groups = get_whatsapp_groups() if cfg.get("service_url") else None
    return render_template(
        "admin/whatsapp.html",
        cfg=cfg,
        status=status,
        groups=groups,
        default_caption_template=DEFAULT_CAPTION_TEMPLATE,
        **_common_admin_context("whatsapp"),
    )


@admin_bp.post("/whatsapp/settings")
@login_required
def whatsapp_save_settings():
    r = _require_admin()
    if r:
        return r
    save_whatsapp_settings(request.form)
    db.session.commit()
    flash("Configurações do WhatsApp salvas.", "success")
    return redirect(url_for("admin.whatsapp_page"))


@admin_bp.post("/whatsapp/test")
@login_required
def whatsapp_test():
    r = _require_admin()
    if r:
        return r
    result = send_whatsapp_test_message(request.form.get("test_message"))
    flash(result.message, "success" if result.ok else "danger")
    return redirect(url_for("admin.whatsapp_page"))


@admin_bp.post("/posts/<int:post_id>/send-whatsapp")
@login_required
def posts_send_whatsapp(post_id):
    r = _require_admin()
    if r:
        return r
    post = Post.query.get_or_404(post_id)
    if not post.published_at or post.published_at > datetime.utcnow():
        flash("Publique a matéria antes de enviar para o WhatsApp.", "warning")
        return redirect(url_for("admin.posts_edit", post_id=post.id))
    result = send_post_to_whatsapp(post)
    if result.ok:
        mark_post_sent(post.id)
    flash(result.message, "success" if result.ok else "danger")
    return redirect(url_for("admin.posts_edit", post_id=post.id))


@admin_bp.get("/settings")
@login_required
def settings_page():
    r = _require_admin()
    if r:
        return r
    selected_top_menu_ids = _selected_top_menu_category_ids()
    return render_template(
        "admin/settings.html",
        live_embed=_setting("live_embed_html", ""),
        logo_url=normalize_media_url(_setting("logo_url", "")),
        favicon_url=normalize_media_url(_setting("favicon_url", "")),
        default_share_image=normalize_media_url(_setting("default_share_image", "")),
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
        all_categories=Category.query.order_by(Category.name.asc()).all(),
        selected_top_menu_ids=selected_top_menu_ids,
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
    logo_url = normalize_media_url((request.form.get("logo_url", "") or "").strip())
    favicon_url = normalize_media_url((request.form.get("favicon_url", "") or "").strip())
    default_share_image = normalize_media_url((request.form.get("default_share_image", "") or "").strip())
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
    selected_top_menu_ids = []
    for raw_id in request.form.getlist('top_menu_category_ids'):
        try:
            selected_top_menu_ids.append(int(raw_id))
        except Exception:
            continue
    _save_setting('top_menu_category_ids', json.dumps(selected_top_menu_ids, ensure_ascii=False))
    _save_setting("logo_url", logo_url)
    _save_setting("favicon_url", favicon_url)
    _save_setting("default_share_image", default_share_image)
    db.session.commit()
    if logo_file and old_logo and normalize_media_url(old_logo) != logo_url:
        _delete_local_media(old_logo)
    if favicon_file and old_favicon and normalize_media_url(old_favicon) != favicon_url:
        _delete_local_media(old_favicon)
    if share_file and old_share and normalize_media_url(old_share) != default_share_image:
        _delete_local_media(old_share)
    flash("Configurações de branding, SEO e compartilhamento atualizadas.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.get("/ads")
@login_required
def ads_editor():
    r = _require_admin()
    if r:
        return r
    slots = AdSlot.query.order_by(AdSlot.key.asc()).all()
    slot_cards = [_slot_card_data(slot) for slot in slots]
    return render_template("admin/ads_editor.html", slot_cards=slot_cards, **_common_admin_context("ads"))


@admin_bp.get("/ads/<int:slot_id>/manage")
@login_required
def ads_manage(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    slot_data = _slot_card_data(slot)
    payload = _slot_visual_payload(slot)
    if not payload['banners']:
        payload['banners'] = [{'title': '', 'link': '', 'image': ''}]
    return render_template("admin/ads_manage.html", slot=slot, slot_data=slot_data, payload=payload, **_common_admin_context("ads"))


@admin_bp.post("/ads/<int:slot_id>/manage")
@login_required
def ads_manage_post(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    slot.name = (request.form.get('name') or slot.name or '').strip() or slot.name
    slot.is_active = bool(request.form.get('is_active'))
    payload, removed_local_images = _build_slot_payload_from_request(slot)
    if not payload['banners']:
        flash('Adicione pelo menos um banner com imagem para esse espaço.', 'danger')
        slot_data = _slot_card_data(slot)
        payload['banners'] = [{'title': '', 'link': '', 'image': ''}]
        return render_template("admin/ads_manage.html", slot=slot, slot_data=slot_data, payload=payload, **_common_admin_context("ads"))
    slot.html = '__ADCFG__' + json.dumps(payload, ensure_ascii=False)
    db.session.commit()
    for image in removed_local_images:
        _delete_local_media(image)
    flash('Publicidade atualizada com sucesso.', 'success')
    return redirect(url_for('admin.ads_manage', slot_id=slot.id))


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
