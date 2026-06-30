from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import current_app

from .models import db, SiteSetting

CACHE_KEY = "worldcup_matches_cache_json"
CACHE_UNTIL_KEY = "worldcup_matches_cache_until"
CACHE_MINUTES = int(os.getenv("WORLDCUP_CACHE_MINUTES", "30") or 30)
OPENFOOTBALL_URL = os.getenv(
    "WORLDCUP_OPENFOOTBALL_URL",
    "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
)
DISPLAY_TIMEZONE = os.getenv("WORLDCUP_TIMEZONE", "America/Sao_Paulo")
MANUAL_JSON_KEY = "worldcup_manual_matches_json"

FINISHED_STATUS = {"FT", "AET", "PEN"}
NOT_STARTED_STATUS = {"NS", "TBD"}

TEAM_TRANSLATIONS = {
    "Algeria": "Argélia",
    "Argentina": "Argentina",
    "Australia": "Austrália",
    "Austria": "Áustria",
    "Belgium": "Bélgica",
    "Bosnia & Herzegovina": "Bósnia e Herzegovina",
    "Brazil": "Brasil",
    "Cameroon": "Camarões",
    "Canada": "Canadá",
    "Cape Verde": "Cabo Verde",
    "Chile": "Chile",
    "Colombia": "Colômbia",
    "Costa Rica": "Costa Rica",
    "Croatia": "Croácia",
    "Curaçao": "Curaçao",
    "Denmark": "Dinamarca",
    "DR Congo": "RD Congo",
    "Ecuador": "Equador",
    "Egypt": "Egito",
    "England": "Inglaterra",
    "France": "França",
    "Germany": "Alemanha",
    "Ghana": "Gana",
    "Iran": "Irã",
    "Italy": "Itália",
    "Ivory Coast": "Costa do Marfim",
    "Japan": "Japão",
    "Mexico": "México",
    "Morocco": "Marrocos",
    "Netherlands": "Holanda",
    "New Zealand": "Nova Zelândia",
    "Norway": "Noruega",
    "Panama": "Panamá",
    "Paraguay": "Paraguai",
    "Peru": "Peru",
    "Poland": "Polônia",
    "Portugal": "Portugal",
    "Qatar": "Catar",
    "Saudi Arabia": "Arábia Saudita",
    "Scotland": "Escócia",
    "Senegal": "Senegal",
    "Serbia": "Sérvia",
    "South Korea": "Coreia do Sul",
    "Spain": "Espanha",
    "Sweden": "Suécia",
    "Switzerland": "Suíça",
    "Turkey": "Turquia",
    "USA": "Estados Unidos",
    "United States": "Estados Unidos",
    "Uruguay": "Uruguai",
    "Wales": "País de Gales",
}

FLAG_EMOJI = {
    "Áustria": "🇦🇹",
    "Argélia": "🇩🇿",
    "Argentina": "🇦🇷",
    "Austrália": "🇦🇺",
    "Bélgica": "🇧🇪",
    "Bósnia e Herzegovina": "🇧🇦",
    "Brasil": "🇧🇷",
    "Cabo Verde": "🇨🇻",
    "Camarões": "🇨🇲",
    "Canadá": "🇨🇦",
    "Catar": "🇶🇦",
    "Chile": "🇨🇱",
    "Colômbia": "🇨🇴",
    "Coreia do Sul": "🇰🇷",
    "Costa Rica": "🇨🇷",
    "Costa do Marfim": "🇨🇮",
    "Croácia": "🇭🇷",
    "Curaçao": "🇨🇼",
    "Dinamarca": "🇩🇰",
    "Egito": "🇪🇬",
    "Equador": "🇪🇨",
    "Escócia": "🏴",
    "Espanha": "🇪🇸",
    "Estados Unidos": "🇺🇸",
    "França": "🇫🇷",
    "Gana": "🇬🇭",
    "Holanda": "🇳🇱",
    "Inglaterra": "🏴",
    "Irã": "🇮🇷",
    "Itália": "🇮🇹",
    "Japão": "🇯🇵",
    "Marrocos": "🇲🇦",
    "México": "🇲🇽",
    "Noruega": "🇳🇴",
    "Nova Zelândia": "🇳🇿",
    "Panamá": "🇵🇦",
    "Paraguai": "🇵🇾",
    "País de Gales": "🏴",
    "Peru": "🇵🇪",
    "Polônia": "🇵🇱",
    "Portugal": "🇵🇹",
    "RD Congo": "🇨🇩",
    "Senegal": "🇸🇳",
    "Sérvia": "🇷🇸",
    "Suécia": "🇸🇪",
    "Suíça": "🇨🇭",
    "Turquia": "🇹🇷",
    "Uruguai": "🇺🇾",
}

ROUND_TRANSLATIONS = {
    "Group Stage": "Fase de grupos",
    "Matchday": "Rodada",
    "Round of 32": "16 avos de final",
    "Round of 16": "Oitavas de final",
    "Quarter-finals": "Quartas de final",
    "Quarterfinals": "Quartas de final",
    "Semi-finals": "Semifinal",
    "Semifinals": "Semifinal",
    "Third-place": "Disputa do 3º lugar",
    "Third Place": "Disputa do 3º lugar",
    "Final": "Final",
}


# Pequeno fallback local para o site nunca exibir card de "configurar API".
# O OpenFootball será a fonte principal sempre que o Railway conseguir acessar a internet.
LOCAL_FALLBACK_MATCHES = [
    {"round": "Matchday 1", "date": "2026-06-11", "time": "19:00 UTC-6", "team1": "Mexico", "team2": "South Africa", "group": "Group A", "ground": "Mexico City"},
    {"round": "Matchday 1", "date": "2026-06-12", "time": "20:00 UTC-4", "team1": "Canada", "team2": "Qatar", "group": "Group B", "ground": "Toronto"},
    {"round": "Matchday 2", "date": "2026-06-13", "time": "18:00 UTC-7", "team1": "Brazil", "team2": "Morocco", "group": "Group C", "ground": "Los Angeles (Inglewood)"},
    {"round": "Matchday 2", "date": "2026-06-13", "time": "21:00 UTC-7", "team1": "Australia", "team2": "Turkey", "score": {"ft": [2, 0], "ht": [1, 0]}, "group": "Group D", "ground": "Vancouver"},
    {"round": "Matchday 4", "date": "2026-06-14", "time": "12:00 UTC-5", "team1": "Germany", "team2": "Curaçao", "score": {"ft": [7, 1], "ht": [3, 1]}, "group": "Group E", "ground": "Kansas City"},
    {"round": "Matchday 6", "date": "2026-06-16", "time": "15:00 UTC-4", "team1": "France", "team2": "Senegal", "score": {"ft": [3, 1], "ht": [0, 0]}, "group": "Group I", "ground": "New York New Jersey"},
    {"round": "Matchday 10", "date": "2026-06-20", "time": "19:00 UTC-5", "team1": "Ecuador", "team2": "Curaçao", "score": {"ft": [0, 0], "ht": [0, 0]}, "group": "Group E", "ground": "Kansas City"},
    {"round": "Matchday 15", "date": "2026-06-25", "time": "18:00 UTC-5", "team1": "Japan", "team2": "Sweden", "score": {"ft": [1, 1], "ht": [0, 0]}, "group": "Group F", "ground": "Monterrey (Guadalupe)"},
    {"round": "Round of 32", "date": "2026-06-30", "time": "12:00 UTC-5", "team1": "Ivory Coast", "team2": "Norway", "ground": "Dallas (Arlington)"},
    {"round": "Round of 32", "date": "2026-06-30", "time": "19:00 UTC-6", "team1": "Mexico", "team2": "Ecuador", "ground": "Mexico City"},
    {"round": "Round of 32", "date": "2026-07-01", "time": "12:00 UTC-4", "team1": "England", "team2": "DR Congo", "ground": "Atlanta"},
    {"round": "Round of 32", "date": "2026-07-02", "time": "19:00 UTC-4", "team1": "Portugal", "team2": "Croatia", "ground": "Toronto"},
    {"round": "Round of 32", "date": "2026-07-03", "time": "18:00 UTC-4", "team1": "Argentina", "team2": "Cape Verde", "ground": "Miami (Miami Gardens)"},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(DISPLAY_TIMEZONE)
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


def _setting(key: str) -> SiteSetting | None:
    return SiteSetting.query.filter_by(key=key).first()


def _get_setting_value(key: str, default: str = "") -> str:
    item = _setting(key)
    return item.value if item and item.value is not None else default


def _set_setting_value(key: str, value: str) -> None:
    item = _setting(key)
    if not item:
        item = SiteSetting(key=key, value=value)
        db.session.add(item)
    else:
        item.value = value


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_openfootball_dt(date_value: str | None, time_value: str | None) -> datetime | None:
    if not date_value:
        return None
    time_text = (time_value or "00:00 UTC").strip()
    match = re.search(r"(\d{1,2}):(\d{2})(?:\s*UTC\s*([+-]?\d{1,2}))?", time_text, re.I)
    hour = int(match.group(1)) if match else 0
    minute = int(match.group(2)) if match else 0
    offset = int(match.group(3) or 0) if match else 0
    try:
        tz = timezone(timedelta(hours=offset))
        return datetime.fromisoformat(date_value).replace(hour=hour, minute=minute, tzinfo=tz)
    except Exception:
        return None


def _team_name(name: str | None) -> str:
    raw = (name or "").strip() or "A definir"
    return TEAM_TRANSLATIONS.get(raw, raw)


def _flag(name: str) -> str:
    return FLAG_EMOJI.get(name, "🏳️")


def _round_label(value: str | None, group: str | None = None) -> str:
    text = (value or "Copa do Mundo").strip()
    for english, portuguese in ROUND_TRANSLATIONS.items():
        text = text.replace(english, portuguese)
    if group:
        text = f"{text} · {group.replace('Group', 'Grupo')}"
    return text


def _status_label(short: str) -> str:
    if short in FINISHED_STATUS:
        return "Encerrado"
    if short == "LIVE":
        return "HOJE"
    if short == "TBD":
        return "A definir"
    return "Pré-jogo"


def _score_values(item: dict[str, Any]) -> tuple[Any, Any, str]:
    score = item.get("score") or {}
    if isinstance(score, dict):
        if isinstance(score.get("ft"), list) and len(score["ft"]) >= 2:
            return score["ft"][0], score["ft"][1], "FT"
        if isinstance(score.get("p"), list) and len(score["p"]) >= 2:
            return score["p"][0], score["p"][1], "PEN"
        if isinstance(score.get("aet"), list) and len(score["aet"]) >= 2:
            return score["aet"][0], score["aet"][1], "AET"
    if isinstance(score, list) and len(score) >= 2:
        return score[0], score[1], "FT"
    return None, None, "NS"


def _normalize_openfootball_match(item: dict[str, Any]) -> dict[str, Any] | None:
    starts_at = _parse_openfootball_dt(item.get("date"), item.get("time"))
    if not starts_at:
        return None

    local_dt = starts_at.astimezone(_display_tz())
    home = _team_name(item.get("team1") or item.get("home") or item.get("home_team"))
    away = _team_name(item.get("team2") or item.get("away") or item.get("away_team"))
    home_score, away_score, status = _score_values(item)

    # Como o OpenFootball é uma base aberta e não um placar minuto a minuto,
    # marcamos jogos do dia como HOJE quando ainda não houver resultado.
    is_today = local_dt.date() == datetime.now(_display_tz()).date()
    if status == "NS" and is_today:
        status = "LIVE"

    stage = _round_label(item.get("round"), item.get("group"))
    ground = item.get("ground") or item.get("venue") or ""

    return {
        "api_id": item.get("num") or f"{item.get('date')}-{home}-{away}",
        "starts_at": starts_at.astimezone(timezone.utc).isoformat(),
        "date_label": local_dt.strftime("%d/%m"),
        "time_label": local_dt.strftime("%H:%M"),
        "home_team": home,
        "away_team": away,
        "home_flag": _flag(home),
        "away_flag": _flag(away),
        "home_logo": "",
        "away_logo": "",
        "home_score": home_score,
        "away_score": away_score,
        "status": status,
        "status_label": _status_label(status),
        "stage": stage,
        "venue": ground,
        "city": "",
        "source": "OpenFootball",
        "is_live": status == "LIVE",
        "is_finished": status in FINISHED_STATUS,
        "is_upcoming": status in NOT_STARTED_STATUS,
        "is_brazil": home == "Brasil" or away == "Brasil",
    }


def _manual_matches() -> list[dict[str, Any]]:
    raw = (os.getenv("WORLDCUP_MANUAL_JSON") or _get_setting_value(MANUAL_JSON_KEY, "")).strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    rows = payload.get("matches") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    matches = []
    for row in rows:
        if isinstance(row, dict):
            normalized = _normalize_openfootball_match(row)
            if normalized:
                normalized["source"] = "Manual"
                matches.append(normalized)
    return matches


def _fetch_openfootball() -> list[dict[str, Any]]:
    response = requests.get(OPENFOOTBALL_URL, timeout=10)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("matches") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    matches = []
    for item in rows:
        if isinstance(item, dict):
            normalized = _normalize_openfootball_match(item)
            if normalized:
                matches.append(normalized)
    return sorted(matches, key=lambda row: row.get("starts_at") or "")


def _fallback_matches() -> list[dict[str, Any]]:
    matches = []
    for item in LOCAL_FALLBACK_MATCHES:
        normalized = _normalize_openfootball_match(item)
        if normalized:
            normalized["source"] = "Fallback local"
            matches.append(normalized)
    return sorted(matches, key=lambda row: row.get("starts_at") or "")


def _merge_manual(base: list[dict[str, Any]], manual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not manual:
        return base
    merged = {str(item.get("api_id")): item for item in base}
    for item in manual:
        merged[str(item.get("api_id"))] = item
    return sorted(merged.values(), key=lambda row: row.get("starts_at") or "")


def _select_home_window(matches: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    now = _now()

    def dt(row: dict[str, Any]) -> datetime:
        parsed = _parse_dt(row.get("starts_at"))
        return parsed or now

    live = [m for m in matches if m.get("is_live")]
    future = [m for m in matches if dt(m) >= now and not m.get("is_live")]
    past = [m for m in matches if dt(m) < now and not m.get("is_live")]
    future.sort(key=dt)
    past.sort(key=dt, reverse=True)

    past_count = min(3, len(past))
    selected = list(reversed(past[:past_count])) + live
    selected.extend(future[: max(0, limit - len(selected))])
    if len(selected) < limit:
        selected.extend([m for m in matches if m not in selected][: limit - len(selected)])
    return selected[:limit]


def get_worldcup_matches(limit: int = 14, home_window: bool = True, force_refresh: bool = False) -> list[dict[str, Any]]:
    cached_until = _parse_dt(_get_setting_value(CACHE_UNTIL_KEY, ""))
    cached_raw = _get_setting_value(CACHE_KEY, "")

    if not force_refresh and cached_until and cached_until > _now() and cached_raw:
        try:
            cached = json.loads(cached_raw)
            if isinstance(cached, list):
                return _select_home_window(cached, limit) if home_window else cached[:limit]
        except Exception:
            pass

    manual = _manual_matches()
    try:
        matches = _merge_manual(_fetch_openfootball(), manual)
        if matches:
            _set_setting_value(CACHE_KEY, json.dumps(matches, ensure_ascii=False))
            _set_setting_value(CACHE_UNTIL_KEY, (_now() + timedelta(minutes=CACHE_MINUTES)).isoformat())
            db.session.commit()
            return _select_home_window(matches, limit) if home_window else matches[:limit]
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("OpenFootball World Cup fetch failed: %s", exc)
        if cached_raw:
            try:
                cached = json.loads(cached_raw)
                if isinstance(cached, list):
                    cached = _merge_manual(cached, manual)
                    return _select_home_window(cached, limit) if home_window else cached[:limit]
            except Exception:
                pass

    fallback = _merge_manual(_fallback_matches(), manual)
    return _select_home_window(fallback, limit) if home_window else fallback[:limit]
