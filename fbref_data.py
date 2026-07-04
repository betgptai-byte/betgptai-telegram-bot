"""Optional FBref enrichment through soccerdata.

FBref/soccerdata can be useful for home/away splits, form, goals, possession,
and team trends.  It can also be slow or blocked, so this module is strictly
best-effort and never required for public cards.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

import pandas as pd

from storage import data_file

UNAVAILABLE = "unavailable"
SOCCERDATA_DIR = data_file(".soccerdata")


def _prepare_soccerdata_env() -> None:
    """Keep soccerdata cache/log writes inside the project workspace."""
    os.environ.setdefault("SOCCERDATA_DIR", str(SOCCERDATA_DIR))
    os.environ.setdefault("SOCCERDATA_LOGLEVEL", "ERROR")


def _normalize_team(name: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    ignored = {"fc", "afc", "cf", "sc", "club", "the"}
    return "".join(word for word in words if word not in ignored)


def fbref_available() -> bool:
    """Return whether soccerdata's FBref reader can be imported."""
    try:
        _prepare_soccerdata_env()
        import soccerdata as sd  # noqa: F401
        return hasattr(sd, "FBref")
    except Exception:
        return False


def _current_fbref_season() -> str:
    """Return a soccerdata season string such as 25-26."""
    today = date.today()
    start_year = today.year if today.month >= 8 else today.year - 1
    return f"{str(start_year)[-2:]}-{str(start_year + 1)[-2:]}"


def _flatten_frame(frame: pd.DataFrame) -> pd.DataFrame:
    copy = frame.copy()
    if isinstance(copy.columns, pd.MultiIndex):
        copy.columns = [
            " ".join(str(part) for part in column if str(part) != "").strip()
            for column in copy.columns
        ]
    else:
        copy.columns = [str(column) for column in copy.columns]
    if isinstance(copy.index, pd.MultiIndex):
        copy = copy.reset_index()
    else:
        copy = copy.reset_index()
    return copy


def _team_row(frame: pd.DataFrame, team_name: str) -> pd.Series | None:
    if frame.empty:
        return None
    target = _normalize_team(team_name)
    for column in frame.columns:
        if any(label in column.lower() for label in ("team", "squad")):
            matches = frame[frame[column].astype(str).map(_normalize_team).eq(target)]
            if not matches.empty:
                return matches.iloc[0]
    # soccerdata often places squad in an index level that becomes a generic
    # reset_index column. Try every object-like column as a final safe fallback.
    for column in frame.columns:
        matches = frame[frame[column].astype(str).map(_normalize_team).eq(target)]
        if not matches.empty:
            return matches.iloc[0]
    return None


def _value(row: pd.Series | None, *aliases: str) -> Any:
    if row is None:
        return UNAVAILABLE
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): key for key in row.index
    }
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if key in normalized:
            value = row[normalized[key]]
            return value if pd.notna(value) else UNAVAILABLE
    return UNAVAILABLE


def _safe_fbref_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read compact FBref season tables when soccerdata can access them."""
    try:
        _prepare_soccerdata_env()
        import soccerdata as sd

        reader = sd.FBref(
            leagues="Big 5 European Leagues Combined",
            seasons=_current_fbref_season(),
            no_cache=False,
            no_store=False,
        )
        standard = _flatten_frame(reader.read_team_season_stats("standard"))
        possession = _flatten_frame(reader.read_team_season_stats("possession"))
        return standard, possession
    except Exception as error:
        logging.warning("FBref/soccerdata enrichment unavailable: %s", error)
        return pd.DataFrame(), pd.DataFrame()


def _fbref_team_context(team_name: str, standard: pd.DataFrame, possession: pd.DataFrame) -> dict[str, Any] | str:
    standard_row = _team_row(standard, team_name)
    possession_row = _team_row(possession, team_name)
    if standard_row is None and possession_row is None:
        return UNAVAILABLE
    return {
        "team": team_name,
        "goals_for": _value(standard_row, "GF", "Standard GF", "Goals For"),
        "goals_against": _value(standard_row, "GA", "Standard GA", "Goals Against"),
        "goal_difference": _value(standard_row, "GD", "Standard GD"),
        "points": _value(standard_row, "Pts", "Standard Pts"),
        "possession": _value(possession_row, "Poss", "Possession Poss", "Poss%"),
        "progressive_passes": _value(possession_row, "PrgP", "Progressive Passes"),
        "touches_attacking_third": _value(possession_row, "Att 3rd", "Touches Att 3rd"),
        "home_away_splits": UNAVAILABLE,
        "last_5_form": UNAVAILABLE,
        "team_trends": "available",
    }


def merge_fbref_data(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach optional FBref context to each soccer fixture."""
    standard, possession = _safe_fbref_frames()
    if standard.empty and possession.empty:
        for game in slate:
            game["fbref_context"] = UNAVAILABLE
        return slate
    for game in slate:
        home = _fbref_team_context(str(game.get("home_team") or ""), standard, possession)
        away = _fbref_team_context(str(game.get("away_team") or ""), standard, possession)
        game["fbref_context"] = (
            {"home_team": home, "away_team": away}
            if home != UNAVAILABLE or away != UNAVAILABLE
            else UNAVAILABLE
        )
    return slate
