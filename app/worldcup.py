from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import current_app

from .models import db, SiteSetting

CACHE_KEY = "worldcup_matches_cache_json"
CACHE_UNTIL_KEY = "worldcup_matches_cache_until"
CACHE_MINUTES = 10
API_BASE = "https://v3.football.api-sports.io"

LIVE_STATUS = {"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"}
FINISHED_STATUS = {"FT", "AET", "PEN"}
NOT_STARTED_STATUS = {"NS", "TBD"}

TEAM_TRANSLATIONS = {
    "Brazil": "Brasil",
    "France": "França",
    "Germany": "Alemanha",
    "England": "Inglaterra",
    "Spain": "Espanha",
    "Portugal": "Portugal",
    "Argentina": "Argentina",
    "Italy": "Itália",
    "Netherlands": "Holanda",
    "Belgium": "Bélgica",
    "Croatia": "Croácia",
    "Uruguay": "Uruguai",
    "Mexico": "México",
    "USA": "Estados Unidos",
    "United States": "Estados Unidos",
    "Canada": "Canadá",
    "Japan": "Japão",
    "South Korea": "Coreia do Sul",
    "Senegal": "Senegal",
    "Morocco": "Marrocos",
    "Ghana": "Gana",
    "Cameroon": "Camarões",
    "Ivory Coast": "Costa do Marfim",
    "Poland": "Polônia",
    "Switzerland": "Suíça",
    "Denmark": "Dinamarca",
    "Sweden": "Suécia",
    "Norway": "Noruega",
    "Ecuador": "Equador",
    "Colombia": "Colômbia",
    "Chile": "Chile",
    "Peru": "Peru",
    "Australia": "Austrália",
    "New Zealand": "Nova Zelândia",
    "Saudi Arabia": "Arábia Saudita",
    "Qatar": "Catar",
    "Iran": "Irã",
    "Wales": "País de Gales",
    "Scotland": "Escócia",
    "Serbia": "Sérvia",
    "Costa Rica": "Costa Rica",
    "Panama": "Panamá",
}

FLAG_EMOJI = {
    "Brasil": "🇧🇷",
    "França": "🇫🇷",
    "Alemanha": "🇩🇪",
    "Inglaterra": "🏴",
    "Espanha": "🇪🇸",
    "Portugal": "🇵🇹",
    "Argentina": "🇦🇷",
    "Itália": "🇮🇹",
    "Holanda": "🇳🇱",
    "Bélgica": "🇧🇪",
    "Croácia": "🇭🇷",
    "Uruguai": "🇺🇾",
    "México": "🇲🇽",
    "Estados Unidos": "🇺🇸",
    "Canadá": "🇨🇦",
    "Japão": "🇯🇵",
    "Coreia do Sul": "🇰🇷",
    "Senegal": "🇸🇳",
    "Marrocos": "🇲🇦",
    "Gana": "🇬🇭",
    "Camarões": "🇨🇲",
    "Costa do Marfim": "🇨🇮",
    "Polônia": "🇵🇱",
    "Suíça": "🇨🇭",
    "Dinamarca": "🇩🇰",
    "Suécia": "🇸🇪",
    "Noruega": "🇳🇴",
    "Equador": "🇪🇨",
    "Colômbia": "🇨🇴",
    "Chile": "🇨🇱",
    "Peru": "🇵🇪",
    "Austrália": "🇦🇺",
    "Nova Zelândia": "🇳🇿",
    "Arábia Saudita": "🇸🇦",
    "Catar": "🇶🇦",
    "Irã": "🇮🇷",
    "País de Gales": "🏴",
    "Escócia": "🏴",
    "Sérvia": "🇷🇸",
    "Costa Rica": "🇨🇷",
    "Panamá": "🇵🇦",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _team_name(name: str | None) -> str:
    raw = (name or "").strip() or "A definir"
    return TEAM_TRANSLATIONS.get(raw, raw)


def _flag(name: str) -> str:
    return FLAG_EMOJI.get(name, "🏳️")


def _round_label(value: str | None) -> str:
    text = (value or "Copa do Mundo").replace("Group Stage", "Fase de grupos")
    text = text.replace("Round of 16", "Oitavas de final")
    text = text.replace("Quarter-finals", "Quartas de final")
    text = text.replace("Semi-finals", "Semifinal")
    text = text.replace("Final", "Final")
    return text


def _normalize_fixture(item: dict[str, Any]) -> dict[str, Any] | None:
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    score = item.get("score") or {}
    league = item.get("league") or {}
    status = fixture.get("status") or {}

    home = _team_name((teams.get("home") or {}).get("name"))
    away = _team_name((teams.get("away") or {}).get("name"))
    starts_at = _parse_dt(fixture.get("date"))
    if not starts_at:
        return None

    short = (status.get("short") or "NS").upper()
    elapsed = status.get("elapsed")
    home_score = goals.get("home")
    away_score = goals.get("away")

    if short == "PEN":
        penalty = score.get("penalty") or {}
        if penalty.get("home") is not None and penalty.get("away") is not None:
            home_score = f'{home_score} ({penalty.get("home")})'
            away_score = f'{away_score} ({penalty.get("away")})'

    return {
        "api_id": fixture.get("id"),
        "starts_at": starts_at.isoformat(),
        "date_label": starts_at.strftime("%d/%m"),
        "time_label": starts_at.strftime("%H:%M"),
        "home_team": home,
        "away_team": away,
        "home_flag": _flag(home),
        "away_flag": _flag(away),
        "home_logo": (teams.get("home") or {}).get("logo") or "",
        "away_logo": (teams.get("away") or {}).get("logo") or "",
        "home_score": home_score,
        "away_score": away_score,
        "status": short,
        "status_label": _status_label(short, elapsed),
        "stage": _round_label(league.get("round")),
        "venue": ((fixture.get("venue") or {}).get("name") or ""),
        "city": ((fixture.get("venue") or {}).get("city") or ""),
        "is_live": short in LIVE_STATUS,
        "is_finished": short in FINISHED_STATUS,
        "is_upcoming": short in NOT_STARTED_STATUS,
        "is_brazil": home == "Brasil" or away == "Brasil",
    }


def _status_label(short: str, elapsed: Any = None) -> str:
    if short in FINISHED_STATUS:
        return "Encerrado"
    if short in LIVE_STATUS:
        suffix = f" {elapsed}'" if elapsed else ""
        return f"AO VIVO{suffix}"
    if short == "TBD":
        return "A definir"
    if short == "PST":
        return "Adiado"
    if short == "CANC":
        return "Cancelado"
    return "Pré-jogo"


def _fetch_api_football() -> list[dict[str, Any]]:
    api_key = (os.getenv("API_FOOTBALL_KEY") or os.getenv("FOOTBALL_API_KEY") or "").strip()
    if not api_key:
        return []

    host = (os.getenv("API_FOOTBALL_HOST") or "v3.football.api-sports.io").strip()
    base_url = (os.getenv("API_FOOTBALL_BASE_URL") or API_BASE).rstrip("/")
    league_id = (os.getenv("WORLDCUP_LEAGUE_ID") or "1").strip()
    season = (os.getenv("WORLDCUP_SEASON") or "2026").strip()
    timezone_name = os.getenv("WORLDCUP_TIMEZONE") or "America/Sao_Paulo"

    response = requests.get(
        f"{base_url}/fixtures",
        params={"league": league_id, "season": season, "timezone": timezone_name},
        headers={"x-apisports-key": api_key, "x-rapidapi-key": api_key, "x-rapidapi-host": host},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    data = payload.get("response") or []
    matches = []
    for item in data:
        if isinstance(item, dict):
            normalized = _normalize_fixture(item)
            if normalized:
                matches.append(normalized)
    return sorted(matches, key=lambda row: row.get("starts_at") or "")


def _fallback_matches() -> list[dict[str, Any]]:
    """Visual fallback shown until an API key is configured."""
    return [
        {
            "api_id": "setup",
            "starts_at": "",
            "date_label": "Copa",
            "time_label": "2026",
            "home_team": "Agenda pronta",
            "away_team": "Conecte a API",
            "home_flag": "🏆",
            "away_flag": "⚽",
            "home_logo": "",
            "away_logo": "",
            "home_score": None,
            "away_score": None,
            "status": "SETUP",
            "status_label": "Configurar API",
            "stage": "API-Football: API_FOOTBALL_KEY no Railway",
            "venue": "",
            "city": "",
            "is_live": False,
            "is_finished": False,
            "is_upcoming": True,
            "is_brazil": True,
            "is_setup": True,
        }
    ]


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

    selected = list(reversed(past[:3])) + live + future[: max(0, limit - len(live) - min(3, len(past)))]
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

    try:
        matches = _fetch_api_football()
        if matches:
            _set_setting_value(CACHE_KEY, json.dumps(matches, ensure_ascii=False))
            _set_setting_value(CACHE_UNTIL_KEY, (_now() + timedelta(minutes=CACHE_MINUTES)).isoformat())
            db.session.commit()
            return _select_home_window(matches, limit) if home_window else matches[:limit]
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("World Cup API fetch failed: %s", exc)
        if cached_raw:
            try:
                cached = json.loads(cached_raw)
                if isinstance(cached, list):
                    return _select_home_window(cached, limit) if home_window else cached[:limit]
            except Exception:
                pass

    return _fallback_matches()
