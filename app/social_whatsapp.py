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


DEFAULT_CAPTION_TEMPLATE = """Nova matéria publicada no Paraná POP 📰

{{titulo}}

{{resumo}}

Link da matéria:
{{url}}

Sugestão de descrição para Instagram/Facebook:
{{resumo}}"""


@dataclass
class WhatsAppResult:
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


def whatsapp_settings() -> dict[str, Any]:
    return {
        "enabled": _setting_bool("whatsapp_enabled", False),
        "auto_send": _setting_bool("whatsapp_auto_send", True),
        "service_url": (_setting("whatsapp_service_url", "") or "").strip().rstrip("/"),
        "default_group_id": (_setting("whatsapp_default_group_id", "") or "").strip(),
        "default_group_name": (_setting("whatsapp_default_group_name", "") or "").strip(),
        "send_feed": _setting_bool("whatsapp_send_feed", True),
        "send_stories": _setting_bool("whatsapp_send_stories", True),
        "send_facebook": _setting_bool("whatsapp_send_facebook", True),
        "caption_template": _setting("whatsapp_caption_template", DEFAULT_CAPTION_TEMPLATE) or DEFAULT_CAPTION_TEMPLATE,
        "bot_publish_enabled": _setting_bool("whatsapp_bot_publish_enabled", False),
        "bot_publish_token": (_setting("whatsapp_bot_publish_token", "") or "").strip(),
        "bot_publish_category_id": (_setting("whatsapp_bot_publish_category_id", "") or "").strip(),
    }


def save_whatsapp_settings(form) -> None:
    _save_setting("whatsapp_enabled", "1" if form.get("whatsapp_enabled") == "on" else "0")
    _save_setting("whatsapp_auto_send", "1" if form.get("whatsapp_auto_send") == "on" else "0")
    _save_setting("whatsapp_service_url", (form.get("whatsapp_service_url") or "").strip().rstrip("/"))
    _save_setting("whatsapp_default_group_id", (form.get("whatsapp_default_group_id") or "").strip())
    _save_setting("whatsapp_default_group_name", (form.get("whatsapp_default_group_name") or "").strip())
    _save_setting("whatsapp_send_feed", "1" if form.get("whatsapp_send_feed") == "on" else "0")
    _save_setting("whatsapp_send_stories", "1" if form.get("whatsapp_send_stories") == "on" else "0")
    _save_setting("whatsapp_send_facebook", "1" if form.get("whatsapp_send_facebook") == "on" else "0")
    _save_setting("whatsapp_caption_template", (form.get("whatsapp_caption_template") or DEFAULT_CAPTION_TEMPLATE).strip())
    _save_setting("whatsapp_bot_publish_enabled", "1" if form.get("whatsapp_bot_publish_enabled") == "on" else "0")
    _save_setting("whatsapp_bot_publish_category_id", (form.get("whatsapp_bot_publish_category_id") or "").strip())


def _client_timeout() -> int:
    try:
        return max(3, int(current_app.config.get("WHATSAPP_SERVICE_TIMEOUT", 25)))
    except Exception:
        return 25


def _service_request(method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> WhatsAppResult:
    cfg = whatsapp_settings()
    base = cfg.get("service_url") or ""
    if not base:
        return WhatsAppResult(False, "URL do serviço WhatsApp não configurada.")
    url = f"{base}/{path.lstrip('/')}"
    try:
        response = requests.request(method, url, json=json_payload, timeout=_client_timeout())
        data: dict[str, Any]
        try:
            data = response.json() if response.content else {}
        except Exception:
            data = {"raw": response.text[:500]}
        if response.ok:
            return WhatsAppResult(True, data.get("message") or "OK", data)
        return WhatsAppResult(False, data.get("message") or data.get("error") or f"Erro HTTP {response.status_code}", data)
    except requests.RequestException as exc:
        return WhatsAppResult(False, f"Falha ao conectar no serviço WhatsApp: {exc}")


def get_whatsapp_status() -> WhatsAppResult:
    return _service_request("GET", "/status")


def get_whatsapp_groups() -> WhatsAppResult:
    return _service_request("GET", "/groups")


def send_whatsapp_test_message(message: str | None = None) -> WhatsAppResult:
    cfg = whatsapp_settings()
    group_id = cfg.get("default_group_id") or ""
    if not group_id:
        return WhatsAppResult(False, "Selecione ou informe um grupo padrão antes de testar.")
    payload = {
        "group_id": group_id,
        "message": message or "Teste de integração do painel Paraná POP ✅",
    }
    return _service_request("POST", "/send-message", json_payload=payload)


def _strip_html(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _post_summary(post: Post, max_len: int = 650) -> str:
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
    result = template or DEFAULT_CAPTION_TEMPLATE
    for key, value in mapping.items():
        result = result.replace(key, value)
    return result.strip()


def _selected_variants(cfg: dict[str, Any]) -> set[str]:
    selected = set()
    if cfg.get("send_feed"):
        selected.add("feed")
    if cfg.get("send_stories"):
        selected.add("stories")
    if cfg.get("send_facebook"):
        selected.add("facebook")
    return selected


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


def build_whatsapp_news_payload(post: Post) -> dict[str, Any]:
    cfg = whatsapp_settings()
    selected = _selected_variants(cfg)
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
        if item.get("key") not in selected:
            continue
        images.append({
            "type": item.get("key"),
            "label": item.get("label"),
            "size": item.get("size"),
            "url": _absolute_media_url(item.get("url") or ""),
        })

    caption = _render_caption(cfg.get("caption_template") or DEFAULT_CAPTION_TEMPLATE, post, post_url, summary)
    description = f"{post.title}\n\n{summary}\n\n{post_url}".strip()

    return {
        "group_id": cfg.get("default_group_id") or "",
        "group_name": cfg.get("default_group_name") or "",
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
        "description": description,
        "send_as_separate_messages": True,
    }


def send_post_to_whatsapp(post: Post) -> WhatsAppResult:
    cfg = whatsapp_settings()
    if not cfg.get("service_url"):
        return WhatsAppResult(False, "URL do serviço WhatsApp não configurada.")
    if not cfg.get("default_group_id"):
        return WhatsAppResult(False, "Grupo padrão do WhatsApp não configurado.")
    payload = build_whatsapp_news_payload(post)
    if not payload.get("images"):
        return WhatsAppResult(False, "Nenhum formato de arte está habilitado para envio.")
    return _service_request("POST", "/send-news", json_payload=payload)


def _sent_ids() -> list[int]:
    raw = _setting_json("whatsapp_sent_post_ids_json", [])
    result = []
    if isinstance(raw, list):
        for item in raw:
            try:
                result.append(int(item))
            except Exception:
                continue
    return result


def mark_post_sent(post_id: int) -> None:
    ids = _sent_ids()
    if int(post_id) not in ids:
        ids.append(int(post_id))
        ids = ids[-1000:]
        _save_setting("whatsapp_sent_post_ids_json", json.dumps(ids))
        _save_setting("whatsapp_last_send_at", datetime.utcnow().isoformat())
        db.session.commit()


def was_post_sent(post_id: int) -> bool:
    return int(post_id) in set(_sent_ids())


def auto_send_post_to_whatsapp(post: Post) -> WhatsAppResult:
    cfg = whatsapp_settings()
    if not cfg.get("enabled") or not cfg.get("auto_send"):
        return WhatsAppResult(True, "Automação WhatsApp desativada.")
    if not post.published_at or post.published_at > datetime.utcnow():
        return WhatsAppResult(True, "Matéria ainda não está publicada.")
    if was_post_sent(post.id):
        return WhatsAppResult(True, "Matéria já enviada anteriormente.")
    result = send_post_to_whatsapp(post)
    if result.ok:
        mark_post_sent(post.id)
    return result
