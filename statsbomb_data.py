"""Optional StatsBomb Free Data enrichment for BETGPTAI soccer.

StatsBomb open data is excellent, but it is not a universal live/current
schedule feed.  This adapter searches the free dataset for matching teams and
adds compact internal summaries when available.  Every failure returns
``unavailable`` so Football-Data.org and TheSportsDB remain the core pipeline.
"""

from __future__ import annotations

import logging
import re
import warnings
from functools import lru_cache
from typing import Any

import pandas as pd


UNAVAILABLE = "unavailable"

try:
    from statsbombpy import sb
    warnings.filterwarnings("ignore", category=UserWarning, module="statsbombpy")
except Exception:  # pragma: no cover - optional dependency path
    sb = None


def _normalize_team(name: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    ignored = {"fc", "afc", "cf", "sc", "club", "the"}
    return "".join(word for word in words if word not in ignored)


def statsbomb_available() -> bool:
    """Return whether the StatsBomb Free Data package can be used."""
    if sb is None:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            competitions = sb.competitions()
        return isinstance(competitions, pd.DataFrame) and not competitions.empty
    except Exception:
        return False


@lru_cache(maxsize=1)
def _competitions() -> pd.DataFrame:
    if sb is None:
        return pd.DataFrame()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = sb.competitions()
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    except Exception as error:
        logging.warning("StatsBomb competitions unavailable: %s", error)
        return pd.DataFrame()


@lru_cache(maxsize=32)
def _matches(competition_id: int, season_id: int) -> pd.DataFrame:
    if sb is None:
        return pd.DataFrame()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = sb.matches(competition_id=competition_id, season_id=season_id)
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    except Exception as error:
        logging.debug("StatsBomb matches unavailable: %s", error)
        return pd.DataFrame()


@lru_cache(maxsize=64)
def _events(match_id: int) -> pd.DataFrame:
    if sb is None:
        return pd.DataFrame()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = sb.events(match_id=match_id)
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    except Exception as error:
        logging.debug("StatsBomb events unavailable: %s", error)
        return pd.DataFrame()


def _candidate_matches(home_team: str, away_team: str, max_competitions: int = 16) -> list[int]:
    """Find a few open-data matches involving either scheduled team."""
    comps = _competitions()
    if comps.empty:
        return []
    required = {"competition_id", "season_id"}
    if not required.issubset(comps.columns):
        return []

    # Search recent seasons first, but keep the scan bounded for bot latency.
    sort_columns = [
        column for column in ("season_name", "competition_id", "season_id")
        if column in comps.columns
    ]
    if sort_columns:
        comps = comps.sort_values(sort_columns, ascending=False)
    home_norm, away_norm = _normalize_team(home_team), _normalize_team(away_team)
    found: list[int] = []
    for _, row in comps.head(max_competitions).iterrows():
        matches = _matches(int(row["competition_id"]), int(row["season_id"]))
        if matches.empty:
            continue
        for _, match in matches.iterrows():
            home = _normalize_team(match.get("home_team"))
            away = _normalize_team(match.get("away_team"))
            if home in {home_norm, away_norm} or away in {home_norm, away_norm}:
                match_id = match.get("match_id")
                if isinstance(match_id, (int, float)):
                    found.append(int(match_id))
            if len(found) >= 4:
                return found
    return found


def _team_event_summary(events: pd.DataFrame, team_name: str) -> dict[str, Any] | str:
    """Create compact internal metrics for one team from StatsBomb events."""
    if events.empty or "team" not in events.columns or "type" not in events.columns:
        return UNAVAILABLE
    team_events = events[
        events["team"].astype(str).map(_normalize_team).eq(_normalize_team(team_name))
    ]
    if team_events.empty:
        return UNAVAILABLE

    def count_type(event_type: str) -> int:
        return int(team_events["type"].astype(str).eq(event_type).sum())

    shots = team_events[team_events["type"].astype(str).eq("Shot")]
    xg = 0.0
    if "shot_statsbomb_xg" in shots.columns:
        xg = float(pd.to_numeric(shots["shot_statsbomb_xg"], errors="coerce").fillna(0).sum())
    goal_minutes: list[int] = []
    if "shot_outcome" in shots.columns and "minute" in shots.columns:
        goal_rows = shots[shots["shot_outcome"].astype(str).str.lower().eq("goal")]
        goal_minutes = [
            int(value) for value in pd.to_numeric(goal_rows["minute"], errors="coerce").dropna().tolist()
        ]
    possession = None
    if "possession_team" in events.columns:
        possession = round(
            100
            * float(events["possession_team"].astype(str).map(_normalize_team).eq(_normalize_team(team_name)).mean()),
            1,
        )
    return {
        "xG": round(xg, 2),
        "shots": int(len(shots)),
        "shots_on_target": int(
            shots.get("shot_outcome", pd.Series(dtype=str)).astype(str).isin(
                ["Goal", "Saved", "Saved to Post"]
            ).sum()
        ),
        "shot_quality": round(xg / len(shots), 3) if len(shots) else UNAVAILABLE,
        "pressures": count_type("Pressure"),
        "passes": count_type("Pass"),
        "defensive_actions": (
            count_type("Duel") + count_type("Interception")
            + count_type("Block") + count_type("Clearance")
        ),
        "possession": possession if possession is not None else UNAVAILABLE,
        "goal_minutes": goal_minutes,
        "late_goals": sum(minute >= 75 for minute in goal_minutes),
        "pace_events": count_type("Shot") + count_type("Pressure") + count_type("Pass"),
    }


def _merge_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any] | str:
    """Average/aggregate a small set of team match summaries."""
    if not summaries:
        return UNAVAILABLE
    count = len(summaries)

    def avg(key: str) -> Any:
        values = [
            float(value) for item in summaries
            if isinstance((value := item.get(key)), (int, float))
        ]
        return round(sum(values) / len(values), 2) if values else UNAVAILABLE

    return {
        "sample_matches": count,
        "xG": avg("xG"),
        "shots": avg("shots"),
        "shots_on_target": avg("shots_on_target"),
        "shot_quality": avg("shot_quality"),
        "pressures": avg("pressures"),
        "passes": avg("passes"),
        "defensive_actions": avg("defensive_actions"),
        "possession": avg("possession"),
        "late_goals": sum(int(item.get("late_goals") or 0) for item in summaries),
        "pace": avg("pace_events"),
        "goal_timing": [
            minute for item in summaries for minute in item.get("goal_minutes", [])
            if isinstance(minute, int)
        ][-8:],
    }


def _game_statsbomb_context(game: dict[str, Any]) -> dict[str, Any] | str:
    home, away = str(game.get("home_team") or ""), str(game.get("away_team") or "")
    if not home or not away:
        return UNAVAILABLE
    match_ids = _candidate_matches(home, away)
    if not match_ids:
        return UNAVAILABLE
    home_summaries: list[dict[str, Any]] = []
    away_summaries: list[dict[str, Any]] = []
    for match_id in match_ids:
        events = _events(match_id)
        home_summary = _team_event_summary(events, home)
        away_summary = _team_event_summary(events, away)
        if isinstance(home_summary, dict):
            home_summaries.append(home_summary)
        if isinstance(away_summary, dict):
            away_summaries.append(away_summary)
    home_context = _merge_summaries(home_summaries)
    away_context = _merge_summaries(away_summaries)
    if home_context == UNAVAILABLE and away_context == UNAVAILABLE:
        return UNAVAILABLE
    return {
        "status": "available",
        "home_team": home_context,
        "away_team": away_context,
        "sample_match_ids": match_ids,
    }


def merge_statsbomb_data(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach optional StatsBomb internal context to each soccer fixture."""
    if sb is None:
        for game in slate:
            game["statsbomb_context"] = UNAVAILABLE
        return slate
    for game in slate:
        try:
            game["statsbomb_context"] = _game_statsbomb_context(game)
        except Exception:
            logging.debug("StatsBomb enrichment failed for one fixture", exc_info=True)
            game["statsbomb_context"] = UNAVAILABLE
    return slate
