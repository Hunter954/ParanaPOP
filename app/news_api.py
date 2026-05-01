from __future__ import annotations

import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class NewsSearchError(RuntimeError):
    pass


def _clean_text(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _source_from_title(title: str) -> tuple[str, str]:
    title = _clean_text(title)
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        return head.strip(), tail.strip()
    return title, ""


def _parse_pubdate(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def _safe_domain(url: str) -> str:
    try:
        host = urlparse(url or "").netloc.lower().replace("www.", "")
        return host
    except Exception:
        return ""


def _extract_first_image_from_html(markup: str) -> str:
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, markup or "", flags=re.I)
        if m:
            image = html.unescape(m.group(1).strip())
            if image.startswith("http://") or image.startswith("https://"):
                return image
    return ""


def _fetch_page_metadata(url: str) -> dict[str, str]:
    if not url or "news.google." in urlparse(url).netloc.lower():
        return {"resolved_url": url or "", "image": "", "description": ""}
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=12, allow_redirects=True)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code >= 400 or "text/html" not in content_type:
            return {"resolved_url": resp.url or url, "image": "", "description": ""}
        text = resp.text[:300000]
        desc = ""
        for pattern in [
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
        ]:
            m = re.search(pattern, text, flags=re.I)
            if m:
                desc = _clean_text(m.group(1))
                break
        return {"resolved_url": resp.url or url, "image": _extract_first_image_from_html(text), "description": desc}
    except Exception:
        return {"resolved_url": url or "", "image": "", "description": ""}


def _get_rss(url: str, provider: str) -> bytes:
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=25, allow_redirects=True)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            resp.raise_for_status()
            if not resp.content or b"<rss" not in resp.content[:1000].lower() and b"<feed" not in resp.content[:1000].lower():
                raise NewsSearchError(f"{provider} retornou resposta sem RSS valido")
            return resp.content
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    raise NewsSearchError(f"{provider}: {last_error}")


def _item_text(item: ET.Element, name: str) -> str:
    value = item.findtext(name)
    if value:
        return value
    # Namespaced RSS/Atom fallbacks.
    for child in item:
        if child.tag.lower().endswith("}" + name.lower()) or child.tag.lower() == name.lower():
            return child.text or ""
    return ""


def _parse_rss_items(payload: bytes, provider: str, limit: int, enrich_images: bool) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    rss_items = root.findall("./channel/item")
    if not rss_items:
        rss_items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(rss_items):
        raw_title = _clean_text(_item_text(item, "title"))
        title, source_name = _source_from_title(raw_title)

        link = _clean_text(_item_text(item, "link"))
        if not link:
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = link_el.attrib.get("href", "")

        source_el = item.find("source")
        source_url = source_el.attrib.get("url", "") if source_el is not None else ""
        if source_el is not None and _clean_text(source_el.text):
            source_name = _clean_text(source_el.text)

        description = _clean_text(_item_text(item, "description") or _item_text(item, "summary") or _item_text(item, "content"))
        pub_date = _parse_pubdate(_item_text(item, "pubDate") or _item_text(item, "published") or _item_text(item, "updated"))
        key = (title or raw_title or "") + "|" + (link or "")
        if key in seen or not (title or raw_title):
            continue
        seen.add(key)

        data = {
            "id": f"news-{provider.lower().replace(' ', '-')}-{int(time.time())}-{idx}",
            "title": title or raw_title,
            "source": source_name or _safe_domain(source_url or link) or "Fonte",
            "source_url": source_url,
            "url": link,
            "description": description,
            "published_at": pub_date,
            "image": "",
            "provider": provider,
        }
        if enrich_images:
            meta = _fetch_page_metadata(link)
            data["url"] = meta.get("resolved_url") or link
            data["image"] = meta.get("image") or ""
            if meta.get("description"):
                data["description"] = meta["description"]
        items.append(data)
        if len(items) >= limit:
            break
    return items


def _build_search_query(query: str, day: str | None = None) -> str:
    query = (query or "").strip()
    if not day:
        return query
    try:
        start = date.fromisoformat(day)
        end = start + timedelta(days=1)
        return f"{query} after:{start.isoformat()} before:{end.isoformat()}"
    except Exception:
        return query


def _provider_urls(search_query: str) -> list[tuple[str, str]]:
    encoded = quote_plus(search_query)
    return [
        (
            "Google News RSS",
            "https://news.google.com/rss/search?"
            f"q={encoded}&hl=pt-BR&gl=BR&ceid=BR:pt-BR",
        ),
        (
            "Google News RSS alternativo",
            "https://news.google.com/rss/search?"
            f"q={encoded}&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        ),
        (
            "Bing News RSS",
            "https://www.bing.com/news/search?"
            f"q={encoded}&format=RSS&cc=BR&setlang=pt-BR",
        ),
    ]


def search_google_news(query: str, day: str | None = None, limit: int = 15, enrich_images: bool = False) -> list[dict[str, Any]]:
    """Busca notícias em RSS.

    O nome da função foi mantido para compatibilidade com o admin.py, mas agora ela
    tenta Google News e, se o Google devolver 503/429, usa Bing News RSS como fallback.
    """
    query = (query or "").strip()
    if not query:
        return []

    search_query = _build_search_query(query, day)
    errors: list[str] = []
    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for provider, url in _provider_urls(search_query):
        try:
            payload = _get_rss(url, provider)
            items = _parse_rss_items(payload, provider=provider, limit=limit, enrich_images=enrich_images)
            for item in items:
                key = ((item.get("title") or "").lower(), (item.get("url") or "").split("?")[0])
                if key in seen:
                    continue
                seen.add(key)
                all_items.append(item)
                if len(all_items) >= limit:
                    return all_items
        except Exception as exc:
            errors.append(str(exc)[:180])
            continue

    if all_items:
        return all_items
    raise NewsSearchError("Nenhum provedor respondeu. " + " | ".join(errors[:3]))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _fallback_article(item: dict[str, Any], site_tone: str = "") -> dict[str, Any]:
    title = _clean_text(item.get("title"))
    source = _clean_text(item.get("source")) or "fonte original"
    url = (item.get("url") or "").strip()
    description = _clean_text(item.get("description"))
    published = _clean_text(item.get("published_at"))
    lead = description or f"A informação foi identificada em levantamento de notícias sobre {title}."
    body = "\n".join([
        f"<p>{html.escape(lead)}</p>",
        "<p>A redação preparou este rascunho em formato jornalístico para revisão antes da publicação. Confira nomes, datas e números diretamente na fonte original antes de publicar.</p>",
        f"<p><strong>Fonte:</strong> <a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener nofollow\">{html.escape(source)}</a>.</p>" if url else f"<p><strong>Fonte:</strong> {html.escape(source)}.</p>",
    ])
    return {
        "titulo": title,
        "subtitulo": lead[:240],
        "resumo": lead[:300],
        "categoria": "Notícias",
        "tags": [],
        "corpo_html": body,
        "fonte_nome": source,
        "fonte_url": url,
        "data_original": published,
    }


def generate_article_with_openai(item: dict[str, Any], site_tone: str = "") -> dict[str, Any]:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return _fallback_article(item, site_tone)

    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    source_payload = {
        "titulo_encontrado": item.get("title") or "",
        "resumo_descricao": item.get("description") or "",
        "fonte": item.get("source") or "",
        "url": item.get("url") or "",
        "data_publicacao": item.get("published_at") or "",
        "tom_do_site": site_tone or "Portal regional brasileiro, linguagem jornalística clara, objetiva e informativa.",
    }
    prompt = (
        "Crie uma matéria jornalística ORIGINAL em português do Brasil, sem copiar frases da fonte. "
        "Use apenas os fatos presentes no payload. Não invente dados, falas, números ou contexto. "
        "Quando faltar informação, escreva de forma cautelosa e deixe claro que é um rascunho para revisão. "
        "Inclua atribuição e link da fonte no final. Retorne somente JSON válido com as chaves: "
        "titulo, subtitulo, resumo, categoria, tags, corpo_html, fonte_nome, fonte_url.\n\n"
        f"PAYLOAD:\n{json.dumps(source_payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": prompt,
                "temperature": 0.35,
                "max_output_tokens": 1800,
            },
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        output_text = data.get("output_text") or ""
        if not output_text:
            parts = []
            for out in data.get("output", []) or []:
                for content in out.get("content", []) or []:
                    if content.get("type") in {"output_text", "text"} and content.get("text"):
                        parts.append(content.get("text"))
            output_text = "\n".join(parts)
        parsed = _extract_json_object(output_text)
        if parsed:
            fallback = _fallback_article(item, site_tone)
            fallback.update({k: v for k, v in parsed.items() if v is not None})
            return fallback
    except Exception as exc:
        fallback = _fallback_article(item, site_tone)
        fallback["erro_ia"] = str(exc)[:220]
        return fallback

    return _fallback_article(item, site_tone)
