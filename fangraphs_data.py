"""Optional FanGraphs enrichment through pybaseball.

FanGraphs is useful, but it sometimes returns HTTP 403 to pybaseball. This
module treats FanGraphs as a bonus data source only:

- It never raises errors into the Telegram bot.
- It caches the last successful download for 24 hours.
- It quietly skips 403/blocked responses.
- It exposes only owner-only status helpers; member cards never mention it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from pybaseball import batting_stats, pitching_stats, team_batting, team_pitching
except ImportError:  # pybaseball is optional at runtime.
    batting_stats = None
    pitching_stats = None
    team_batting = None
    team_pitching = None


BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "fangraphs_cache.json"
CACHE_SECONDS = 24 * 60 * 60
FAILURE_COOLDOWN_SECONDS = 24 * 60 * 60
HEALTH_SECONDS = 10 * 60
UNAVAILABLE = "unavailable"

_MEMORY_CACHE: dict[int, dict[str, Any]] = {}
_HEALTH_CACHE: tuple[float, bool] | None = None
_LAST_FAILURE_AT: float | None = None

TEAM_ABBREVIATIONS = {
    "Arizona Diamondbacks": "ARI",
    "Athletics": "ATH",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CHW",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KCR",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SDP",
    "San Francisco Giants": "SFG",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TBR",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSN",
}


def _normalize(value: Any) -> str:
    """Normalize names so team abbreviations and punctuation match safely."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Make pybaseball DataFrame columns easy to serialize and search."""
    copy = frame.copy()
    if isinstance(copy.columns, pd.MultiIndex):
        copy.columns = [
            " ".join(str(part) for part in column if "Unnamed" not in str(part)).strip()
            for column in copy.columns
        ]
    else:
        copy.columns = [str(column).strip() for column in copy.columns]
    return copy


def _column(frame: pd.DataFrame, *aliases: str) -> str | None:
    """Find a column despite minor naming changes in pybaseball/FanGraphs."""
    if frame.empty:
        return None
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(name).lower()): str(name)
        for name in frame.columns
    }
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if key in normalized:
            return normalized[key]
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if len(key) < 3:
            continue
        for normalized_name, original in normalized.items():
            if key in normalized_name or normalized_name in key:
                return original
    return None


def _value(row: pd.Series | None, frame: pd.DataFrame, *aliases: str) -> Any:
    """Read one metric and keep unavailable fields explicit."""
    if row is None or frame.empty:
        return UNAVAILABLE
    name = _column(frame, *aliases)
    if not name:
        return UNAVAILABLE
    value = row.get(name)
    if pd.isna(value):
        return UNAVAILABLE
    return value.item() if hasattr(value, "item") else value


def _find_player(frame: pd.DataFrame, player_name: str) -> pd.Series | None:
    """Find a player row by exact normalized name, then unambiguous last name."""
    name_column = _column(frame, "Name", "Player", "player_name")
    if not name_column or not player_name:
        return None
    target = _normalize(player_name)
    matches = frame[frame[name_column].map(_normalize) == target]
    if matches.empty:
        last = player_name.split()[-1] if player_name.split() else player_name
        matches = frame[
            frame[name_column].astype(str).str.lower().str.contains(
                rf"\b{re.escape(last.lower())}\b", regex=True, na=False
            )
        ]
    return matches.iloc[0] if len(matches) == 1 else None


def _find_team(frame: pd.DataFrame, team_name: str) -> pd.Series | None:
    """Find a team row by abbreviation first, then full team name."""
    team_column = _column(frame, "Team", "team_name", "Name")
    if not team_column or not team_name:
        return None
    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    normalized_targets = {_normalize(abbreviation), _normalize(team_name)}
    matches = frame[frame[team_column].map(_normalize).isin(normalized_targets)]
    return matches.iloc[0] if len(matches) == 1 else None


def _frame_to_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Serialize a DataFrame compactly for the disk cache."""
    if frame.empty:
        return []
    cleaned = frame.where(pd.notnull(frame), None)
    return cleaned.to_dict(orient="records")


def _rows_to_frame(rows: Any) -> pd.DataFrame:
    """Rebuild a DataFrame from cached rows."""
    return pd.DataFrame(rows if isinstance(rows, list) else [])


def _read_disk_cache(season: int) -> dict[str, Any] | None:
    """Return a fresh disk cache payload, if one exists."""
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    season_payload = payload.get(str(season)) if isinstance(payload, dict) else None
    if not isinstance(season_payload, dict):
        return None
    fetched_at = float(season_payload.get("fetched_at", 0) or 0)
    if time.time() - fetched_at > CACHE_SECONDS:
        return None
    return season_payload


def _write_disk_cache(season: int, data: dict[str, Any]) -> None:
    """Save the latest successful FanGraphs data without risking bot failure."""
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        if not isinstance(payload, dict):
            payload = {}
        payload[str(season)] = data
        temporary = CACHE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        temporary.replace(CACHE_FILE)
    except OSError:
        logging.debug("Could not write FanGraphs cache", exc_info=True)


def _call_fangraphs(loader: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
    """Call one pybaseball FanGraphs loader and normalize expected failures."""
    if loader is None:
        return pd.DataFrame()
    try:
        frame = loader(*args, **kwargs)
        if not isinstance(frame, pd.DataFrame):
            return pd.DataFrame()
        return _flatten_columns(frame)
    except Exception as error:
        # HTTP 403 and similar blocks are expected. Keep them out of member
        # output and normal logs unless debug logging is enabled.
        if "403" in str(error):
            logging.debug("FanGraphs returned HTTP 403; using cache/skip.")
        else:
            logging.debug("FanGraphs/pybaseball unavailable; using cache/skip.", exc_info=True)
        return pd.DataFrame()


def _download_fangraphs(season: int) -> dict[str, pd.DataFrame] | None:
    """Download the supported FanGraphs tables once per cache window."""
    if not all((pitching_stats, batting_stats, team_batting, team_pitching)):
        return None
    pitching = _call_fangraphs(pitching_stats, season, season, qual=0)
    batting = _call_fangraphs(batting_stats, season, season, qual=0)
    teams_batting = _call_fangraphs(team_batting, season, season, qual=0)
    teams_pitching = _call_fangraphs(team_pitching, season, season, qual=0)
    if all(frame.empty for frame in (pitching, batting, teams_batting, teams_pitching)):
        return None
    return {
        "pitching": pitching,
        "batting": batting,
        "team_batting": teams_batting,
        "team_pitching": teams_pitching,
    }


def get_fangraphs_dataset(season: int) -> dict[str, pd.DataFrame] | None:
    """Return cached FanGraphs data, refreshing only after 24 hours."""
    global _LAST_FAILURE_AT
    if season in _MEMORY_CACHE:
        cached = _MEMORY_CACHE[season]
        if time.time() - float(cached.get("fetched_at", 0) or 0) <= CACHE_SECONDS:
            return {
                "pitching": _rows_to_frame(cached.get("pitching")),
                "batting": _rows_to_frame(cached.get("batting")),
                "team_batting": _rows_to_frame(cached.get("team_batting")),
                "team_pitching": _rows_to_frame(cached.get("team_pitching")),
            }

    disk = _read_disk_cache(season)
    if disk:
        _MEMORY_CACHE[season] = disk
        return {
            "pitching": _rows_to_frame(disk.get("pitching")),
            "batting": _rows_to_frame(disk.get("batting")),
            "team_batting": _rows_to_frame(disk.get("team_batting")),
            "team_pitching": _rows_to_frame(disk.get("team_pitching")),
        }

    # When FanGraphs blocks pybaseball, avoid hammering it on every command.
    if _LAST_FAILURE_AT and time.time() - _LAST_FAILURE_AT <= FAILURE_COOLDOWN_SECONDS:
        return None

    downloaded = _download_fangraphs(season)
    if not downloaded:
        _LAST_FAILURE_AT = time.time()
        return None
    _LAST_FAILURE_AT = None
    serializable = {
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pitching": _frame_to_rows(downloaded["pitching"]),
        "batting": _frame_to_rows(downloaded["batting"]),
        "team_batting": _frame_to_rows(downloaded["team_batting"]),
        "team_pitching": _frame_to_rows(downloaded["team_pitching"]),
    }
    _MEMORY_CACHE[season] = serializable
    _write_disk_cache(season, serializable)
    return downloaded


def _pitcher_metrics(dataset: dict[str, pd.DataFrame], pitcher_name: str) -> dict[str, Any] | str:
    """Return supported FanGraphs pitcher metrics for one starter."""
    frame = dataset.get("pitching", pd.DataFrame())
    row = _find_player(frame, pitcher_name)
    if row is None:
        return UNAVAILABLE
    return {
        "xFIP": _value(row, frame, "xFIP"),
        "SIERA": _value(row, frame, "SIERA"),
        "K-BB%": _value(row, frame, "K-BB%"),
        "K%": _value(row, frame, "K%"),
        "BB%": _value(row, frame, "BB%"),
        "HR/9": _value(row, frame, "HR/9"),
        "GB%": _value(row, frame, "GB%"),
        "Hard%": _value(row, frame, "Hard%", "HardHit%"),
        "WAR": _value(row, frame, "WAR"),
    }


def _team_batting_metrics(dataset: dict[str, pd.DataFrame], team_name: str) -> dict[str, Any] | str:
    """Return supported FanGraphs team hitting metrics."""
    frame = dataset.get("team_batting", pd.DataFrame())
    row = _find_team(frame, team_name)
    if row is None:
        return UNAVAILABLE
    return {
        "wRC+": _value(row, frame, "wRC+"),
        "wOBA": _value(row, frame, "wOBA"),
        "ISO": _value(row, frame, "ISO"),
        "OPS": _value(row, frame, "OPS"),
        "BB%": _value(row, frame, "BB%"),
        "K%": _value(row, frame, "K%"),
        "Hard%": _value(row, frame, "Hard%", "HardHit%"),
        "Pull%": _value(row, frame, "Pull%"),
        "WAR": _value(row, frame, "WAR"),
    }


def _team_pitching_metrics(dataset: dict[str, pd.DataFrame], team_name: str) -> dict[str, Any] | str:
    """Return supported FanGraphs team/bullpen-style pitching metrics."""
    frame = dataset.get("team_pitching", pd.DataFrame())
    row = _find_team(frame, team_name)
    if row is None:
        return UNAVAILABLE
    return {
        "xFIP": _value(row, frame, "xFIP"),
        "K-BB%": _value(row, frame, "K-BB%"),
        "ERA": _value(row, frame, "ERA"),
        "WHIP": _value(row, frame, "WHIP"),
        "HR/9": _value(row, frame, "HR/9"),
    }


def _hitter_samples(dataset: dict[str, pd.DataFrame], team_name: str, limit: int = 5) -> list[dict[str, Any]] | str:
    """Return a compact top-hitter sample for internal model context."""
    frame = dataset.get("batting", pd.DataFrame())
    team_column = _column(frame, "Team")
    if frame.empty or not team_column:
        return UNAVAILABLE
    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    rows = frame[frame[team_column].map(_normalize).isin({_normalize(abbreviation), _normalize(team_name)})]
    if rows.empty:
        return UNAVAILABLE
    sort_column = _column(rows, "wRC+", "WAR", "wOBA")
    if sort_column:
        rows = rows.assign(_fg_score=pd.to_numeric(rows[sort_column], errors="coerce")).sort_values(
            "_fg_score", ascending=False, na_position="last"
        )
    name_column = _column(rows, "Name", "Player")
    samples = []
    for _, row in rows.head(limit).iterrows():
        samples.append({
            "player": row.get(name_column) if name_column else UNAVAILABLE,
            "wRC+": _value(row, rows, "wRC+"),
            "wOBA": _value(row, rows, "wOBA"),
            "ISO": _value(row, rows, "ISO"),
            "OPS": _value(row, rows, "OPS"),
            "Hard%": _value(row, rows, "Hard%", "HardHit%"),
            "WAR": _value(row, rows, "WAR"),
        })
    return samples or UNAVAILABLE


def merge_fangraphs_data(games: list[dict[str, Any]], selected_date: str) -> list[dict[str, Any]]:
    """Attach optional FanGraphs context to MLB games without blocking cards."""
    try:
        season = int(selected_date[:4])
    except (TypeError, ValueError):
        season = datetime.now().year
    dataset = get_fangraphs_dataset(season)
    if not dataset:
        for game in games:
            game.setdefault("fangraphs", UNAVAILABLE)
        return games

    for game in games:
        away_team = str(game.get("away_team", ""))
        home_team = str(game.get("home_team", ""))
        away_pitcher = str(game.get("away_pitcher", "TBD"))
        home_pitcher = str(game.get("home_pitcher", "TBD"))
        game["fangraphs"] = {
            "away_pitcher": _pitcher_metrics(dataset, away_pitcher) if away_pitcher != "TBD" else UNAVAILABLE,
            "home_pitcher": _pitcher_metrics(dataset, home_pitcher) if home_pitcher != "TBD" else UNAVAILABLE,
            "away_team_batting": _team_batting_metrics(dataset, away_team),
            "home_team_batting": _team_batting_metrics(dataset, home_team),
            "away_team_pitching": _team_pitching_metrics(dataset, away_team),
            "home_team_pitching": _team_pitching_metrics(dataset, home_team),
            "away_hitter_samples": _hitter_samples(dataset, away_team),
            "home_hitter_samples": _hitter_samples(dataset, home_team),
            "cache_ttl_hours": 24,
        }
    return games


def fangraphs_available() -> bool:
    """Lightweight owner-only FanGraphs status check with a short health cache."""
    global _HEALTH_CACHE
    now = time.monotonic()
    if _HEALTH_CACHE and now - _HEALTH_CACHE[0] <= HEALTH_SECONDS:
        return _HEALTH_CACHE[1]
    if not all((pitching_stats, batting_stats, team_batting, team_pitching)):
        _HEALTH_CACHE = (now, False)
        return False
    season = datetime.now().year
    dataset = get_fangraphs_dataset(season)
    available = bool(dataset and any(not frame.empty for frame in dataset.values()))
    _HEALTH_CACHE = (now, available)
    return available
