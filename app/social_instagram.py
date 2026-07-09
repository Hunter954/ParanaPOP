from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from flask import current_app, url_for

from .art_generator import generate_variants
from .models import Post, SiteSetting, db
from .storage import normalize_media_url


DEFAULT_INSTAGRAM_CAPTION_TEMPLATE = """{{titulo}}

{{resumo}}

Leia a matéria completa no Paraná POP:
{{url}}

#ParanaPOP #Noticias #FozDoIguacu #Parana"""


@dataclass
class InstagramResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


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
    raw = (_setting(key, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "sim"}


def _setting_json(key: str, default):
    raw = (_setting(key, "") or "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def instagram_settings() -> dict[str, Any]:
    return {
        "enabled": _setting_bool("instagram_bot_enabled", False),
        "auto_send": _setting_bool("instagram_bot_auto_send", False),
        "service_url": (_setting("instagram_bot_service_url", "") or "").strip().rstrip("/"),
        "service_token": (_setting("instagram_bot_service_token", "") or "").strip(),
        "send_feed": _setting_bool("instagram_bot_send_feed", True),
        "send_story": _setting_bool("instagram_bot_send_story", False),
        "caption_template": _setting("instagram_bot_caption_template", DEFAULT_INSTAGRAM_CAPTION_TEMPLATE) or DEFAULT_INSTAGRAM_CAPTION_TEMPLATE,
        "sent_post_ids": _setting_json("instagram_bot_sent_post_ids_json", []),
    }


def save_instagram_settings(form) -> None:
    _save_setting("instagram_bot_enabled", "1" if form.get("instagram_bot_enabled") == "on" else "0")
    _save_setting("instagram_bot_auto_send", "1" if form.get("instagram_bot_auto_send") == "on" else "0")
    _save_setting("instagram_bot_service_url", (form.get("instagram_bot_service_url") or "").strip().rstrip("/"))
    _save_setting("instagram_bot_service_token", (form.get("instagram_bot_service_token") or "").strip())
    _save_setting("instagram_bot_send_feed", "1" if form.get("instagram_bot_send_feed") == "on" else "0")
    _save_setting("instagram_bot_send_story", "1" if form.get("instagram_bot_send_story") == "on" else "0")
    _save_setting("instagram_bot_caption_template", (form.get("instagram_bot_caption_template") or DEFAULT_INSTAGRAM_CAPTION_TEMPLATE).strip())


def _client_timeout() -> int:
    try:
        return max(3, int(current_app.config.get("INSTAGRAM_SERVICE_TIMEOUT", 60)))
    except Exception:
        return 60


def _service_request(method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> InstagramResult:
    cfg = instagram_settings()
    base = cfg.get("service_url") or ""
    if not base:
        return InstagramResult(False, "URL do serviço Instagram não configurada.")
    url = f"{base}/{path.lstrip('/')}"
    headers = {}
    if cfg.get("service_token"):
        headers["X-Service-Token"] = cfg["service_token"]
    try:
        response = requests.request(method, url, json=json_payload, headers=headers, timeout=_client_timeout())
        try:
            data = response.json() if response.content else {}
        except Exception:
            data = {"raw": response.text[:500]}
        if response.ok:
            return InstagramResult(True, data.get("message") or "OK", data)
        return InstagramResult(False, data.get("message") or data.get("error") or f"Erro HTTP {response.status_code}", data)
    except requests.RequestException as exc:
        return InstagramResult(False, f"Falha ao conectar no serviço Instagram: {exc}")


def get_instagram_status() -> InstagramResult:
    return _service_request("GET", "/status")


def login_instagram_service(username: str, password: str, verification_code: str = "") -> InstagramResult:
    payload = {
        "username": (username or "").strip(),
        "password": password or "",
        "verification_code": (verification_code or "").strip(),
    }
    return _service_request("POST", "/login", json_payload=payload)


def send_instagram_test_message() -> InstagramResult:
    return _service_request("POST", "/test", json_payload={"message": "Teste do painel Paraná POP"})


def _strip_html(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _post_summary(post: Post, max_len: int = 450) -> str:
    summary = _strip_html(post.excerpt) or _strip_html(post.content_html)
    if len(summary) > max_len:
        summary = summary[: max_len - 1].rsplit(" ", 1)[0].strip() + "…"
    return summary


def public_post_url(post: Post) -> str:
    try:
        return url_for("site.post", slug=post.slug, _external=True)
    except Exception:
        base = (current_app.config.get("WP_BASE_URL") or "https://www.paranapop.com.br").rstrip("/")
        return f"{base}/p/{post.slug}"


def _render_caption(template: str, post: Post, post_url: str, summary: str) -> str:
    category = post.categories[0].name if post.categories else ""
    mapping = {
        "{{titulo}}": post.title or "",
        "{{resumo}}": summary or "",
        "{{url}}": post_url or "",
        "{{categoria}}": category or "",
    }
    result = template or DEFAULT_INSTAGRAM_CAPTION_TEMPLATE
    for key, value in mapping.items():
        result = result.replace(key, value)
    return result.strip()


def _absolute_media_url(url: str) -> str:
    value = normalize_media_url(url or "")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    try:
        return url_for("site.home", _external=True).rstrip("/") + "/" + value.lstrip("/")
    except Exception:
        base = (current_app.config.get("WP_BASE_URL") or "https://www.paranapop.com.br").rstrip("/")
        return f"{base}/{value.lstrip('/')}"


def build_instagram_news_payload(post: Post) -> dict[str, Any]:
    cfg = instagram_settings()
    category = post.categories[0].name if post.categories else ""
    summary = _post_summary(post)
    post_url = public_post_url(post)
    image_source = normalize_media_url(post.featured_image or "")

    generated = generate_variants(
        title=post.title or "",
        image_source=image_source,
        include_title=True,
        category_text=category,
    )

    images = []
    for item in generated:
        key = item.get("key")
        if key == "feed" and cfg.get("send_feed"):
            images.append({
                "type": "feed",
                "label": item.get("label"),
                "size": item.get("size"),
                "url": _absolute_media_url(item.get("url") or ""),
            })
        if key == "stories" and cfg.get("send_story"):
            images.append({
                "type": "story",
                "label": item.get("label"),
                "size": item.get("size"),
                "url": _absolute_media_url(item.get("url") or ""),
            })

    caption = _render_caption(cfg.get("caption_template") or DEFAULT_INSTAGRAM_CAPTION_TEMPLATE, post, post_url, summary)

    return {
        "post": {
            "id": post.id,
            "title": post.title or "",
            "summary": summary,
            "url": post_url,
            "category": category,
            "published_at": post.published_at.isoformat() if post.published_at else "",
        },
        "images": images,
        "caption": caption,
    }


def send_post_to_instagram(post: Post) -> InstagramResult:
    cfg = instagram_settings()
    if not cfg.get("service_url"):
        return InstagramResult(False, "URL do serviço Instagram não configurada.")
    payload = build_instagram_news_payload(post)
    if not payload.get("images"):
        return InstagramResult(False, "Nenhum formato de arte está habilitado para o Instagram.")
    return _service_request("POST", "/publish", json_payload=payload)


def _sent_ids() -> list[int]:
    raw = _setting_json("instagram_bot_sent_post_ids_json", [])
    result = []
    if isinstance(raw, list):
        for item in raw:
            try:
                result.append(int(item))
            except Exception:
                continue
    return result


def mark_post_sent_instagram(post_id: int) -> None:
    ids = _sent_ids()
    if int(post_id) not in ids:
        ids.append(int(post_id))
        ids = ids[-1000:]
        _save_setting("instagram_bot_sent_post_ids_json", json.dumps(ids))
        _save_setting("instagram_bot_last_send_at", datetime.utcnow().isoformat())
        db.session.commit()


def was_post_sent_instagram(post_id: int) -> bool:
    return int(post_id) in set(_sent_ids())


def auto_send_post_to_instagram(post: Post) -> InstagramResult:
    cfg = instagram_settings()
    if not cfg.get("enabled") or not cfg.get("auto_send"):
        return InstagramResult(True, "Automação Instagram desativada.")
    if not post.published_at or post.published_at > datetime.utcnow():
        return InstagramResult(True, "Matéria ainda não está publicada.")
    if was_post_sent_instagram(post.id):
        return InstagramResult(True, "Matéria já publicada anteriormente no Instagram.")
    result = send_post_to_instagram(post)
    if result.ok:
        mark_post_sent_instagram(post.id)
    return result
