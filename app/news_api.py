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
from urllib.parse import quote_plus, urlparse, unquote

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


def _absolute_url(base_url: str, maybe_url: str) -> str:
    maybe_url = html.unescape((maybe_url or "").strip())
    if not maybe_url:
        return ""
    if maybe_url.startswith("http://") or maybe_url.startswith("https://"):
        return maybe_url
    if maybe_url.startswith("//"):
        return "https:" + maybe_url
    try:
        from urllib.parse import urljoin
        return urljoin(base_url, maybe_url)
    except Exception:
        return maybe_url


def _extract_meta_value(markup: str, names: list[str]) -> str:
    for name in names:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            m = re.search(pattern, markup or "", flags=re.I)
            if m:
                return _clean_text(m.group(1))
    return ""


def _is_google_news_url(url: str) -> bool:
    host = _safe_domain(url)
    return host in {"news.google.com", "google.com"} or host.endswith(".google.com") and "/articles/" in (url or "")


def _looks_like_placeholder_image(url: str) -> bool:
    """Evita salvar logos/cards genéricos como se fossem foto da matéria."""
    value = (url or "").strip().lower()
    if not value:
        return True
    host = _safe_domain(value)
    bad_bits = [
        "news.google.com", "gstatic.com/images/branding", "googlelogo", "google-news", "googlenews",
        "favicon", "logo", "sprite", "placeholder", "default-image", "default.png", "blank.gif",
    ]
    if any(bit in value for bit in bad_bits):
        return True
    if host in {"www.google.com", "google.com", "news.google.com"}:
        return True
    return False


def _extract_first_image_from_html(markup: str, base_url: str = "") -> str:
    markup = markup or ""
    patterns = [
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)',
        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)',
        r'"image"\s*:\s*"(https?:\\/\\/[^"\\]+)',
        r'<img[^>]+(?:data-src|data-original|src)=["\']([^"\']+)["\']',
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for m in re.finditer(pattern, markup, flags=re.I | re.S):
            raw = (m.group(1) or "").replace("\\/", "/")
            image = _absolute_url(base_url, raw)
            if image.startswith("http://") or image.startswith("https://"):
                candidates.append(image)
    # srcset costuma trazer a foto principal em portais que usam lazy loading.
    for m in re.finditer(r'<img[^>]+(?:data-srcset|srcset)=["\']([^"\']+)["\']', markup, flags=re.I | re.S):
        srcset = html.unescape(m.group(1) or "")
        first = srcset.split(",")[0].strip().split(" ")[0]
        image = _absolute_url(base_url, first)
        if image.startswith("http://") or image.startswith("https://"):
            candidates.append(image)
    for image in candidates:
        if not _looks_like_placeholder_image(image):
            return image
    return ""


def _resolve_google_news_url(url: str) -> str:
    """Tenta sair do agregador do Google News e chegar na URL real da fonte."""
    if not _is_google_news_url(url):
        return url or ""
    try:
        headers = dict(REQUEST_HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        resp = requests.get(url, headers=headers, timeout=18, allow_redirects=True)
        final_url = resp.url or url
        if final_url and not _is_google_news_url(final_url):
            return final_url
        text = resp.text or ""
        decoded_text = html.unescape(unquote(text))
        patterns = [
            r'data-n-au=["\'](https?://[^"\']+)',
            r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*?(?:rel=["\']nofollow|target=["\']_blank)',
            r'url=(https?://[^"\'&<>]+)',
            r'(https?://(?:www\.)?(?!news\.google\.com|www\.google\.com|accounts\.google\.com|support\.google\.com)[^\s"\'<>\\]+)',
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, decoded_text, flags=re.I | re.S):
                candidate = html.unescape(unquote((m.group(1) or "").strip()))
                candidate = candidate.split("&")[0]
                if candidate.startswith("http") and not _is_google_news_url(candidate) and "google." not in _safe_domain(candidate):
                    return candidate
    except Exception:
        pass
    return url or ""


def _extract_readable_text(markup: str) -> str:
    text = markup or ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", text, flags=re.I | re.S)
    clean_parts = []
    for paragraph in paragraphs:
        part = _clean_text(paragraph)
        lower = part.lower()
        if len(part) < 45:
            continue
        if any(skip in lower for skip in ["cookies", "newsletter", "assine", "publicidade", "continua após", "todos os direitos"]):
            continue
        clean_parts.append(part)
        if sum(len(x) for x in clean_parts) >= 5500:
            break
    if not clean_parts:
        text = _clean_text(text)
        return text[:5500]
    return "\n".join(clean_parts)[:5500]


def _fetch_page_metadata(url: str, include_text: bool = False) -> dict[str, str]:
    if not url:
        return {"resolved_url": "", "image": "", "description": "", "article_text": ""}
    original_url = url
    url = _resolve_google_news_url(url)
    try:
        headers = dict(REQUEST_HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Referer"] = "https://www.google.com/"
        resp = requests.get(url, headers=headers, timeout=22, allow_redirects=True)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        resolved_url = resp.url or url
        if resp.status_code >= 400 or ("text/html" not in content_type and "application/xhtml" not in content_type):
            return {"resolved_url": resolved_url, "image": "", "description": "", "article_text": ""}
        text = resp.text[:900000]
        # Se ainda caiu numa página do Google News, tenta extrair a URL real uma segunda vez.
        if _is_google_news_url(resolved_url):
            candidate = _resolve_google_news_url(resolved_url)
            if candidate and candidate != resolved_url and not _is_google_news_url(candidate):
                return _fetch_page_metadata(candidate, include_text=include_text)
        desc = _extract_meta_value(text, ["description", "og:description", "twitter:description"])
        title = _extract_meta_value(text, ["og:title", "twitter:title"])
        article_text = _extract_readable_text(text) if include_text else ""
        image = _extract_first_image_from_html(text, resolved_url)
        return {
            "resolved_url": resolved_url if resolved_url else original_url,
            "image": "" if _looks_like_placeholder_image(image) else image,
            "description": desc,
            "page_title": title,
            "article_text": article_text,
        }
    except Exception:
        return {"resolved_url": url or original_url or "", "image": "", "description": "", "article_text": ""}


def _extract_rss_image(item: ET.Element) -> str:
    # Funciona para Bing/Google e feeds com media:thumbnail, media:content, enclosure etc.
    for child in item.iter():
        tag = (child.tag or "").lower()
        if tag.endswith("}thumbnail") or tag.endswith("}content") or tag.endswith("thumbnail") or tag.endswith("content"):
            url = child.attrib.get("url") or child.attrib.get("href") or ""
            medium = (child.attrib.get("medium") or "").lower()
            if url and (not medium or medium == "image"):
                return html.unescape(url.strip())
        if tag.endswith("}enclosure") or tag.endswith("enclosure"):
            url = child.attrib.get("url") or ""
            typ = (child.attrib.get("type") or "").lower()
            if url and (typ.startswith("image/") or not typ):
                return html.unescape(url.strip())
    description = _item_text(item, "description") or _item_text(item, "summary") or ""
    return _extract_first_image_from_html(description)


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
            "image": _extract_rss_image(item),
            "provider": provider,
            "image_status": "rss",
        }
        if _looks_like_placeholder_image(data.get("image") or ""):
            data["image"] = ""
            data["image_status"] = "sem_imagem"
        if enrich_images:
            meta = _fetch_page_metadata(link)
            data["url"] = meta.get("resolved_url") or link
            if meta.get("image"):
                data["image"] = meta["image"]
                data["image_status"] = "fonte_original"
            if meta.get("description"):
                data["description"] = meta["description"]
            if meta.get("page_title") and len(data["title"]) < 8:
                data["title"] = meta["page_title"]
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
        except Exception as exc:
            errors.append(str(exc)[:180])
            continue

    if all_items:
        # Depois que todos os provedores responderem, prioriza itens com imagem,
        # porque o Google News RSS muitas vezes não entrega thumb, enquanto Bing/feeds sim.
        provider_rank = {"Google News RSS": 0, "Google News RSS alternativo": 1, "Bing News RSS": 2}
        all_items.sort(key=lambda it: (0 if it.get("image") else 1, provider_rank.get(it.get("provider") or "", 9)))
        return all_items[:limit]
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


def enrich_news_item(item: dict[str, Any], include_text: bool = True) -> dict[str, Any]:
    """Completa URL final, imagem, descrição e um recorte textual para orientar a IA.

    O recorte não é salvo nem copiado integralmente; ele serve apenas como base de fatos
    para a geração de uma matéria original.
    """
    enriched = dict(item or {})
    meta = _fetch_page_metadata((enriched.get("url") or "").strip(), include_text=include_text)
    if meta.get("resolved_url"):
        enriched["url"] = meta["resolved_url"]
    if _looks_like_placeholder_image(enriched.get("image") or ""):
        enriched["image"] = ""
    if meta.get("image"):
        enriched["image"] = meta["image"]
        enriched["image_status"] = "fonte_original"
    if meta.get("description"):
        enriched["description"] = meta["description"]
    if meta.get("article_text"):
        enriched["article_text"] = meta["article_text"]
    if meta.get("page_title") and len((enriched.get("title") or "").strip()) < 8:
        enriched["title"] = meta["page_title"]
    return enriched


def generate_article_with_openai(item: dict[str, Any], site_tone: str = "") -> dict[str, Any]:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return _fallback_article(item, site_tone)

    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    source_payload = {
        "titulo_encontrado": item.get("title") or "",
        "resumo_descricao": item.get("description") or "",
        "recorte_textual_da_fonte_para_apuracao": (item.get("article_text") or "")[:5500],
        "fonte": item.get("source") or "",
        "url": item.get("url") or "",
        "data_publicacao": item.get("published_at") or "",
        "tom_do_site": site_tone or "Portal regional brasileiro, linguagem jornalística clara, objetiva e informativa.",
    }
    prompt = (
        "Crie uma matéria jornalística ORIGINAL em português do Brasil. Não copie trechos, frases ou a estrutura da fonte. "
        "Use o recorte textual apenas para entender os fatos principais e reescreva com linguagem própria. "
        "A matéria deve ser mais completa do que um resumo curto, com 7 a 10 parágrafos, lead forte, contexto, detalhes confirmados e fechamento. "
        "Não invente dados, falas, números, acusações ou informações não presentes no payload. "
        "Quando faltar informação, use formulações cautelosas e evite afirmar como fato. "
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
                "max_output_tokens": 3200,
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



def test_openai_connection() -> dict[str, Any]:
    """Verifica rapidamente se a chave da OpenAI está configurada e respondendo."""
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "message": "OPENAI_API_KEY não está configurada no Railway."}
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": "Responda somente: ok", "max_output_tokens": 10},
            timeout=20,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message") or resp.text
            except Exception:
                detail = resp.text
            return {"ok": False, "message": f"Erro OpenAI {resp.status_code}: {detail[:220]}", "model": model}
        return {"ok": True, "message": f"GPT conectado usando {model}.", "model": model}
    except Exception as exc:
        return {"ok": False, "message": f"Falha ao testar OpenAI: {str(exc)[:220]}", "model": model}
