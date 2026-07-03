"""Optional Baseball Savant/Statcast enrichment powered by pybaseball.

pybaseball is the primary adapter. Its upstream pages can still change, so a
small compatibility parser is retained and all failures become ``unavailable``
instead of breaking a BETGPTAI card.
"""

from __future__ import annotations

import logging
import json
import re
import time
from datetime import date, timedelta
from io import StringIO
from typing import Any

import pandas as pd
import requests

try:
    from pybaseball import (
        statcast_batter_expected_stats,
        statcast_batter_exitvelo_barrels,
        statcast,
        statcast_pitcher_arsenal_stats,
        statcast_pitcher_exitvelo_barrels,
        statcast_pitcher_expected_stats,
        statcast_pitcher_percentile_ranks,
        statcast_pitcher_pitch_arsenal,
    )
except ImportError:  # A clear optional failure is safer than stopping the bot.
    statcast_batter_expected_stats = None
    statcast_batter_exitvelo_barrels = None
    statcast = None
    statcast_pitcher_arsenal_stats = None
    statcast_pitcher_exitvelo_barrels = None
    statcast_pitcher_expected_stats = None
    statcast_pitcher_percentile_ranks = None
    statcast_pitcher_pitch_arsenal = None


SAVANT_BASE_URL = "https://baseballsavant.mlb.com"
REQUEST_TIMEOUT = 25
UNAVAILABLE = "unavailable"
PREDICTIVE_METRICS = ("xERA", "xwOBA", "Barrel %", "Whiff %")

# Savant tables commonly use abbreviations while MLB Stats uses full names.
TEAM_ABBREVIATIONS = {
    "Arizona Diamondbacks": "ARI", "Athletics": "ATH", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS", "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS", "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET", "Houston Astros": "HOU",
    "Kansas City Royals": "KC", "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN",
    "New York Mets": "NYM", "New York Yankees": "NYY", "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL", "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}

_TABLE_CACHE: dict[tuple[str, int], pd.DataFrame] = {}
_RECENT_STATCAST_CACHE: dict[tuple[str, str], pd.DataFrame] = {}
_HEALTH_CACHE: tuple[float, bool] | None = None


def _pybaseball_table(kind: str, season: int) -> pd.DataFrame:
    """Use pybaseball's documented Savant helpers for season leaderboards."""
    loaders = {
        "pitcher_expected": lambda: statcast_pitcher_expected_stats(season, minPA=1),
        "batter_expected": lambda: statcast_batter_expected_stats(season, minPA=1),
        "pitcher_batted_ball": lambda: statcast_pitcher_exitvelo_barrels(season, minBBE=1),
        "batter_batted_ball": lambda: statcast_batter_exitvelo_barrels(season, minBBE=1),
        "pitch_arsenal": lambda: statcast_pitcher_arsenal_stats(season, minPA=1),
        "pitch_velocity": lambda: statcast_pitcher_pitch_arsenal(
            season, minP=1, arsenal_type="avg_speed"
        ),
    }
    loader = loaders.get(kind)
    if loader is None:
        raise ValueError(f"No pybaseball loader is defined for {kind}.")
    frame = loader()
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"pybaseball returned no {kind} data.")
    return _flatten_columns(frame)


def _normal(value: Any) -> str:
    """Normalize a player/team label so harmless punctuation does not matter."""
    text = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    return " ".join(sorted(text.split()))


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Turn multi-row leaderboard headings into simple strings."""
    copy = frame.copy()
    if isinstance(copy.columns, pd.MultiIndex):
        copy.columns = [" ".join(str(part) for part in column if "Unnamed" not in str(part)).strip()
                        for column in copy.columns]
    else:
        copy.columns = [str(column).strip() for column in copy.columns]
    return copy


def _compatibility_table(kind: str, season: int) -> pd.DataFrame:
    """Read Savant's embedded payload only when pybaseball cannot supply it."""
    urls = {
        "pitcher_expected": f"/leaderboard/expected_statistics?type=pitcher&year={season}&min=1",
        "batter_expected": f"/leaderboard/expected_statistics?type=batter&year={season}&min=1",
        # The expected-stat pages also expose Barrel%, Hard-Hit%, EV, launch
        # angle, and sweet-spot rate in their embedded public payload.
        "pitcher_batted_ball": f"/leaderboard/expected_statistics?type=pitcher&year={season}&min=1",
        "batter_batted_ball": f"/leaderboard/expected_statistics?type=batter&year={season}&min=1",
        "pitcher_custom": f"/leaderboard/custom?type=pitcher&year={season}&min=1",
        "pitch_arsenal": f"/leaderboard/pitch-arsenal-stats?type=pitcher&year={season}&min=1",
        "pitch_velocity": f"/leaderboard/pitch-arsenal-stats?type=avg_speed&year={season}&min=1",
        "league": f"/league?season={season}",
    }
    response = requests.get(
        SAVANT_BASE_URL + urls[kind], timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "BETGPTAI/1.2 (public Statcast leaderboard reader)"},
    )
    response.raise_for_status()
    # Savant renders its grid in JavaScript but places the complete JSON array
    # in the HTML as ``var data = [...]``. Reading that public payload is much
    # less brittle than pretending the client-rendered grid is an HTML table.
    marker = re.search(r"\bvar\s+data\s*=\s*", response.text)
    if marker:
        payload, _ = json.JSONDecoder().raw_decode(response.text[marker.end():])
        frame = pd.DataFrame(payload)
    else:
        # Some secondary leaderboards still expose a conventional table.
        tables = pd.read_html(StringIO(response.text), flavor="lxml")
        if not tables:
            raise ValueError(f"Baseball Savant returned no {kind} table.")
        frame = max(tables, key=lambda item: item.shape[0] * item.shape[1])
    frame = _flatten_columns(frame)
    return frame


def _download_table(kind: str, season: int) -> pd.DataFrame:
    """Load a Savant table through pybaseball, then its compatibility fallback."""
    key = (kind, season)
    if key in _TABLE_CACHE:
        return _TABLE_CACHE[key]
    if kind not in {"league", "pitcher_custom"}:
        try:
            frame = _pybaseball_table(kind, season)
            # pybaseball's expected-batter result has player IDs but not team
            # identity. Merge only that small metadata field from Savant so
            # team summaries remain possible without replacing pybaseball.
            if kind == "batter_expected" and "player_id" in frame.columns:
                try:
                    metadata = _compatibility_table(kind, season)
                    id_column = _column(metadata, "entity_id", "player_id")
                    team_column = _column(metadata, "entity_team_name_alt", "Team")
                    if id_column and team_column:
                        teams = metadata[[id_column, team_column]].rename(
                            columns={id_column: "player_id", team_column: "entity_team_name_alt"}
                        )
                        frame["player_id"] = frame["player_id"].astype(str)
                        teams["player_id"] = teams["player_id"].astype(str)
                        frame = frame.merge(teams.drop_duplicates("player_id"), on="player_id", how="left")
                except Exception as error:
                    logging.warning("Savant batter-team metadata unavailable: %s", error)
            _TABLE_CACHE[key] = frame
            return frame
        except Exception as error:
            logging.warning("pybaseball %s lookup failed; using fallback: %s", kind, error)
    frame = _compatibility_table(kind, season)
    _TABLE_CACHE[key] = frame
    return frame


def _column(frame: pd.DataFrame, *aliases: str) -> str | None:
    """Find a column despite small naming changes in Savant's web UI."""
    normalized = {re.sub(r"[^a-z0-9]", "", name.lower()): name for name in frame.columns}
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if key in normalized:
            return normalized[key]
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if len(key) < 4:
            continue
        for normalized_name, original in normalized.items():
            if len(normalized_name) >= 4 and (key in normalized_name or normalized_name in key):
                return original
    return None


def _value(row: pd.Series, frame: pd.DataFrame, *aliases: str) -> Any:
    name = _column(frame, *aliases)
    if not name:
        return UNAVAILABLE
    value = row.get(name)
    if pd.isna(value):
        return UNAVAILABLE
    return value.item() if hasattr(value, "item") else value


def _find_row(frame: pd.DataFrame, value: str, *name_aliases: str) -> pd.Series | None:
    name_column = _column(frame, *name_aliases)
    if not name_column or not value:
        return None
    target = _normal(value)
    matches = frame[frame[name_column].map(_normal) == target]
    if matches.empty:
        # Last-name fallback is useful for ``Last, First`` headings, but only
        # when it produces one unambiguous player.
        last = value.split()[-1].lower()
        matches = frame[frame[name_column].astype(str).str.lower().str.contains(
            rf"\b{re.escape(last)}\b", regex=True, na=False
        )]
    return None if len(matches) != 1 else matches.iloc[0]


def _safe_table(kind: str, season: int) -> pd.DataFrame:
    try:
        return _download_table(kind, season)
    except Exception as error:
        logging.warning("Baseball Savant %s leaderboard unavailable: %s", kind, error)
        empty = pd.DataFrame()
        _TABLE_CACHE[(kind, season)] = empty
        return empty


def _pitcher_velocity(player_name: str, season: int) -> Any:
    """Return four-seam velocity, falling back to sinker velocity if needed."""
    frame = _safe_table("pitch_velocity", season)
    row = _find_row(
        frame, player_name, "last_name, first_name", "player_name", "entity_name"
    ) if not frame.empty else None
    if row is None:
        return UNAVAILABLE
    return _value(row, frame, "ff_avg_speed", "si_avg_speed", "Fastball Velocity")


def _fastball_velocity_trend(player_name: str, season: int) -> dict[str, Any] | str:
    """Compare current-season fastball velocity with the previous season."""
    current = _pitcher_velocity(player_name, season)
    previous = _pitcher_velocity(player_name, season - 1)
    try:
        change = round(float(current) - float(previous), 1)
    except (TypeError, ValueError):
        return UNAVAILABLE
    return {
        "current_season_mph": current,
        "previous_season_mph": previous,
        "change_mph": change,
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
    }


def get_pitcher_metrics(player_name: str, season: int) -> dict[str, Any] | str:
    """Return expected-contact, swing, and velocity metrics for one pitcher."""
    expected = _safe_table("pitcher_expected", season)
    batted = _safe_table("pitcher_batted_ball", season)
    custom = _safe_table("pitcher_custom", season)
    rows = {
        "expected": _find_row(expected, player_name, "Player", "Pitcher", "entity_name", "last_name, first_name") if not expected.empty else None,
        "batted": _find_row(batted, player_name, "Player", "Pitcher", "entity_name", "last_name, first_name") if not batted.empty else None,
        "custom": _find_row(custom, player_name, "Player", "Pitcher", "entity_name", "player_name") if not custom.empty else None,
    }
    if not any(row is not None for row in rows.values()):
        return UNAVAILABLE

    def metric(source: str, *aliases: str) -> Any:
        row, frame = rows[source], {"expected": expected, "batted": batted, "custom": custom}[source]
        return _value(row, frame, *aliases) if row is not None else UNAVAILABLE

    # Expected-stat tables already contain the contact-quality fields. They
    # are the clean fallback when the dedicated contact leaderboard changes.
    contact_row = rows["batted"] if rows["batted"] is not None else rows["expected"]
    contact_frame = batted if rows["batted"] is not None else expected
    return {
        "xERA": metric("expected", "xERA"),
        "xBA": metric("expected", "xBA", "est_ba"),
        "xSLG": metric("expected", "xSLG", "est_slg"),
        "Barrel %": _value(contact_row, contact_frame, "Barrel %", "Barrel/BBE", "barrels_per_bip", "brl_percent"),
        "Hard Hit %": _value(contact_row, contact_frame, "Hard Hit %", "HardHit%", "hard_hit_percent", "ev95percent"),
        "Exit Velocity": _value(contact_row, contact_frame, "Exit Velocity", "Avg EV", "exit_velocity_avg", "avg_hit_speed"),
        "Chase %": metric("custom", "Chase %", "Chase Rate", "oz_swing_percent"),
        "Whiff %": metric("custom", "Whiff %", "Whiff Rate", "whiff_percent"),
        "Fastball Velocity": _pitcher_velocity(player_name, season),
        "Fastball Velocity Trend": _fastball_velocity_trend(player_name, season),
    }


def _batter_row_metrics(row: pd.Series, expected: pd.DataFrame, batted: pd.DataFrame) -> dict[str, Any]:
    """Build the supported batter metric set from two leaderboard rows."""
    name = _value(row, expected, "Player", "Batter", "entity_name", "last_name, first_name")
    contact = _find_row(
        batted, str(name), "Player", "Batter", "entity_name", "last_name, first_name"
    ) if name != UNAVAILABLE and not batted.empty else None
    contact_row = contact if contact is not None else row
    contact_frame = batted if contact is not None else expected
    return {
        "player": name,
        "player_id": _value(row, expected, "player_id", "entity_id", "batter"),
        "team": _value(row, expected, "Team", "Tm", "entity_team_name_alt"),
        "xwOBA": _value(row, expected, "xwOBA", "est_woba"),
        "Barrel %": _value(contact_row, contact_frame, "Barrel %", "Barrel/BBE", "barrels_per_bip", "brl_percent"),
        "Hard Hit %": _value(contact_row, contact_frame, "Hard Hit %", "HardHit%", "hard_hit_percent", "ev95percent"),
        "Exit Velocity": _value(contact_row, contact_frame, "Exit Velocity", "Avg EV", "exit_velocity_avg", "avg_hit_speed"),
        "Sweet Spot %": _value(contact_row, contact_frame, "Sweet Spot %", "SweetSpot%", "sweet_spot_percent"),
        "Launch Angle": _value(contact_row, contact_frame, "Launch Angle", "launch_angle_avg", "avg_hit_angle"),
    }


def get_team_batter_metrics(team_name: str, season: int, limit: int = 5) -> list[dict[str, Any]] | str:
    """Return a compact list of the club's strongest available xwOBA batters."""
    expected = _safe_table("batter_expected", season)
    batted = _safe_table("batter_batted_ball", season)
    team_column = _column(expected, "Team", "Tm", "entity_team_name_alt") if not expected.empty else None
    if expected.empty or not team_column:
        return UNAVAILABLE
    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    candidates = expected[expected[team_column].astype(str).map(_normal) == _normal(abbreviation)]
    if candidates.empty:
        return UNAVAILABLE
    xwoba = _column(candidates, "xwOBA", "est_woba")
    if xwoba:
        candidates = candidates.assign(_score=pd.to_numeric(candidates[xwoba], errors="coerce")).sort_values(
            "_score", ascending=False, na_position="last"
        )
    return [_batter_row_metrics(row, expected, batted) for _, row in candidates.head(limit).iterrows()]


def _team_xwoba_splits(
    team_name: str, season: int, selected_date: str | None
) -> dict[str, Any]:
    """Calculate recent team xwOBA versus L/R pitchers from pybaseball pitches."""
    if statcast is None:
        return {"xwOBA vs LHP": UNAVAILABLE, "xwOBA vs RHP": UNAVAILABLE}
    requested = date.fromisoformat(selected_date) if selected_date else date.today()
    end = min(requested - timedelta(days=1), date.today() - timedelta(days=1))
    start = max(date(season, 3, 15), end - timedelta(days=13))
    if end < start:
        return {"xwOBA vs LHP": UNAVAILABLE, "xwOBA vs RHP": UNAVAILABLE}
    key = (start.isoformat(), end.isoformat())
    if key not in _RECENT_STATCAST_CACHE:
        try:
            _RECENT_STATCAST_CACHE[key] = statcast(
                start.isoformat(), end.isoformat(), verbose=False, parallel=True
            )
        except Exception as error:
            logging.warning("pybaseball handedness split unavailable: %s", error)
            _RECENT_STATCAST_CACHE[key] = pd.DataFrame()
    pitches = _RECENT_STATCAST_CACHE[key]
    required = {
        "inning_topbot", "away_team", "home_team", "p_throws",
        "woba_value", "woba_denom", "estimated_woba_using_speedangle",
    }
    if pitches.empty or not required.issubset(pitches.columns):
        return {"xwOBA vs LHP": UNAVAILABLE, "xwOBA vs RHP": UNAVAILABLE}
    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    batting_team = pitches["away_team"].where(
        pitches["inning_topbot"].astype(str).str.lower().eq("top"), pitches["home_team"]
    )
    rows = pitches[batting_team.astype(str).eq(abbreviation)].copy()
    rows["woba_denom"] = pd.to_numeric(rows["woba_denom"], errors="coerce")
    rows["woba_value"] = pd.to_numeric(rows["woba_value"], errors="coerce")
    rows["expected_woba"] = pd.to_numeric(
        rows["estimated_woba_using_speedangle"], errors="coerce"
    ).fillna(rows["woba_value"])

    def split(hand: str) -> Any:
        sample = rows[(rows["p_throws"] == hand) & (rows["woba_denom"] > 0)]
        denominator = sample["woba_denom"].sum()
        if not denominator:
            return UNAVAILABLE
        return round(float((sample["expected_woba"] * sample["woba_denom"]).sum() / denominator), 3)

    return {
        "xwOBA vs LHP": split("L"),
        "xwOBA vs RHP": split("R"),
        "handedness_sample": f"{start.isoformat()} through {end.isoformat()}",
    }


def get_team_metrics(
    team_name: str, season: int, selected_date: str | None = None
) -> dict[str, Any] | str:
    """Read team contact quality, including handedness splits when exposed."""
    league = _safe_table("league", season)

    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    if not league.empty:
        row = _find_row(league, abbreviation, "Team", "Tm", "entity_name", "entity_team_name_alt")
        if row is None:
            row = _find_row(league, team_name, "Team", "Tm", "entity_name", "entity_team_name_alt")
        if row is not None:
            return {
                "xwOBA": _value(row, league, "xwOBA", "est_woba"),
                "xwOBA vs LHP": _value(row, league, "xwOBA vs LHP", "xwOBA LHP"),
                "xwOBA vs RHP": _value(row, league, "xwOBA vs RHP", "xwOBA RHP"),
                "Hard Hit %": _value(row, league, "Hard Hit %", "HardHit%", "hard_hit_percent"),
                "Barrel %": _value(row, league, "Barrel %", "Barrel/BBE", "barrels_per_bip"),
            }

    # Fallback: aggregate individual expected-stat rows for the team. The
    # handedness splits stay unavailable rather than being guessed.
    batters = _safe_table("batter_expected", season)
    team_column = _column(batters, "Team", "Tm", "entity_team_name_alt") if not batters.empty else None
    if not team_column:
        return UNAVAILABLE
    rows = batters[batters[team_column].astype(str).map(_normal) == _normal(abbreviation)]
    if rows.empty:
        return UNAVAILABLE

    def average(*aliases: str, weight_aliases: tuple[str, ...] = ("PA", "pa")) -> Any:
        column = _column(rows, *aliases)
        if not column:
            return UNAVAILABLE
        values = pd.to_numeric(rows[column], errors="coerce").dropna()
        weight_column = _column(rows, *weight_aliases)
        if values.empty:
            return UNAVAILABLE
        if weight_column:
            weights = pd.to_numeric(rows.loc[values.index, weight_column], errors="coerce").fillna(0)
            if weights.sum() > 0:
                return round(float((values * weights).sum() / weights.sum()), 3)
        return round(float(values.mean()), 3)

    contact_rows = pd.DataFrame()
    contacts = _safe_table("batter_batted_ball", season)
    if "player_id" in rows.columns and "player_id" in contacts.columns:
        ids = set(rows["player_id"].astype(str))
        contact_rows = contacts[contacts["player_id"].astype(str).isin(ids)]

    def contact_average(*aliases: str) -> Any:
        if contact_rows.empty:
            return UNAVAILABLE
        column = _column(contact_rows, *aliases)
        if not column:
            return UNAVAILABLE
        values = pd.to_numeric(contact_rows[column], errors="coerce").dropna()
        weight_column = _column(contact_rows, "attempts", "BIP")
        if values.empty:
            return UNAVAILABLE
        if weight_column:
            weights = pd.to_numeric(contact_rows.loc[values.index, weight_column], errors="coerce").fillna(0)
            if weights.sum() > 0:
                return round(float((values * weights).sum() / weights.sum()), 3)
        return round(float(values.mean()), 3)

    splits = _team_xwoba_splits(team_name, season, selected_date)
    return {
        "xwOBA": average("xwOBA", "est_woba"),
        **splits,
        "Hard Hit %": contact_average("Hard Hit %", "hard_hit_percent", "ev95percent"),
        "Barrel %": contact_average("Barrel %", "barrels_per_bip", "brl_percent"),
    }


def get_bullpen_metrics(team_name: str, season: int) -> dict[str, Any] | str:
    """Return bullpen fields only when Savant's current team table supplies them.

    xFIP and WHIP are not native Statcast expected metrics, so they are not
    estimated here. The optional FanGraphs module enriches those separately.
    """
    league = _safe_table("league", season)
    if league.empty:
        return UNAVAILABLE
    abbreviation = TEAM_ABBREVIATIONS.get(team_name, team_name)
    row = _find_row(league, abbreviation, "Team", "Tm", "entity_name", "entity_team_name_alt")
    if row is None:
        row = _find_row(league, team_name, "Team", "Tm", "entity_name", "entity_team_name_alt")
    if row is None:
        return UNAVAILABLE
    return {
        "xFIP": _value(row, league, "Bullpen xFIP", "xFIP"),
        "WHIP": _value(row, league, "Bullpen WHIP", "WHIP"),
        "K-BB%": _value(row, league, "Bullpen K-BB%", "K-BB%"),
        "Hard Hit %": _value(row, league, "Pitching Hard Hit %", "Hard Hit %"),
    }


def get_pitch_type_matchup(pitcher_name: str, opponent_team: str, season: int) -> dict[str, Any] | str:
    """Return the pitcher's supplied arsenal for downstream matchup analysis."""
    # The custom leaderboard exposes usage and velocity by pitch in one stable
    # payload, so it is the preferred source for a compact arsenal summary.
    custom = _safe_table("pitcher_custom", season)
    row = _find_row(custom, pitcher_name, "Player", "Pitcher", "player_name") if not custom.empty else None
    if row is not None:
        pitch_names = {
            "ff": "Four-Seam Fastball", "si": "Sinker", "fc": "Cutter",
            "sl": "Slider", "st": "Sweeper", "cu": "Curveball",
            "ch": "Changeup", "fs": "Splitter",
        }
        pitches = []
        for code, label in pitch_names.items():
            velocity = _value(row, custom, f"{code}_avg_speed")
            count = _value(row, custom, f"n_{code}_formatted", f"pitch_count_{code}")
            if velocity != UNAVAILABLE or count != UNAVAILABLE:
                pitches.append({
                    "pitch_type": label, "usage_count": count,
                    "velocity": velocity, "whiff_rate": UNAVAILABLE,
                    "xwOBA_allowed": UNAVAILABLE,
                })
        return ({"pitcher": pitcher_name, "opponent": opponent_team, "arsenal": pitches}
                if pitches else UNAVAILABLE)

    # Retain a flexible parser for Savant's dedicated arsenal grid as a backup.
    arsenal = _safe_table("pitch_arsenal", season)
    if arsenal.empty:
        return UNAVAILABLE
    name_column = _column(arsenal, "Player", "Pitcher", "entity_name")
    if not name_column:
        return UNAVAILABLE
    target = _normal(pitcher_name)
    rows = arsenal[arsenal[name_column].map(_normal) == target]
    if rows.empty:
        return UNAVAILABLE
    pitch_column = _column(arsenal, "Pitch Type", "Pitch", "Pitch Name")
    usage_column = _column(arsenal, "Usage %", "Pitch %", "Usage")
    whiff_column = _column(arsenal, "Whiff %", "Whiff")
    xwoba_column = _column(arsenal, "xwOBA")
    velocity_column = _column(arsenal, "Velocity", "Velo")
    pitches = []
    for _, row in rows.head(8).iterrows():
        pitches.append({
            "pitch_type": row.get(pitch_column) if pitch_column else UNAVAILABLE,
            "usage": row.get(usage_column) if usage_column else UNAVAILABLE,
            "whiff_rate": row.get(whiff_column) if whiff_column else UNAVAILABLE,
            "xwOBA_allowed": row.get(xwoba_column) if xwoba_column else UNAVAILABLE,
            "velocity": row.get(velocity_column) if velocity_column else UNAVAILABLE,
        })
    return {"pitcher": pitcher_name, "opponent": opponent_team, "arsenal": pitches}


def merge_savant_data(games: list[dict[str, Any]], selected_date: str) -> list[dict[str, Any]]:
    """Attach compact Savant context to every game without changing core data."""
    season = int(selected_date[:4])
    pitcher_cache: dict[str, Any] = {}
    batter_cache: dict[str, Any] = {}
    team_cache: dict[str, Any] = {}
    bullpen_cache: dict[str, Any] = {}
    arsenal_cache: dict[tuple[str, str], Any] = {}

    for game in games:
        away_team, home_team = str(game.get("away_team", "")), str(game.get("home_team", ""))
        away_pitcher, home_pitcher = str(game.get("away_pitcher", "TBD")), str(game.get("home_pitcher", "TBD"))
        for pitcher in (away_pitcher, home_pitcher):
            if pitcher not in pitcher_cache:
                pitcher_cache[pitcher] = get_pitcher_metrics(pitcher, season) if pitcher != "TBD" else UNAVAILABLE
        for team in (away_team, home_team):
            if team not in batter_cache:
                batter_cache[team] = get_team_batter_metrics(team, season)
                team_cache[team] = get_team_metrics(team, season, selected_date)
                bullpen_cache[team] = get_bullpen_metrics(team, season)
        for pitcher, opponent in ((away_pitcher, home_team), (home_pitcher, away_team)):
            key = (pitcher, opponent)
            if key not in arsenal_cache:
                arsenal_cache[key] = get_pitch_type_matchup(pitcher, opponent, season) if pitcher != "TBD" else UNAVAILABLE

        game["savant"] = {
            "predictive_priority": list(PREDICTIVE_METRICS),
            "away_pitcher": pitcher_cache[away_pitcher],
            "home_pitcher": pitcher_cache[home_pitcher],
            "away_batters": batter_cache[away_team],
            "home_batters": batter_cache[home_team],
            "away_team": team_cache[away_team],
            "home_team": team_cache[home_team],
            "away_bullpen": bullpen_cache[away_team],
            "home_bullpen": bullpen_cache[home_team],
            "pitch_type_matchups": {
                "away_pitcher_vs_home": arsenal_cache[(away_pitcher, home_team)],
                "home_pitcher_vs_away": arsenal_cache[(home_pitcher, away_team)],
            },
        }
    return games


def savant_available() -> bool:
    """Run a lightweight pybaseball connectivity check with a five-minute cache.

    The broad exception handler is intentional: /status is diagnostic and a
    temporary Savant or pybaseball failure must never crash the Telegram bot.
    """
    global _HEALTH_CACHE
    now = time.monotonic()
    if _HEALTH_CACHE and now - _HEALTH_CACHE[0] < 300:
        return _HEALTH_CACHE[1]
    if statcast_pitcher_expected_stats is None:
        _HEALTH_CACHE = (now, False)
        return False
    try:
        # One compact season leaderboard request is enough to verify both
        # pybaseball and Baseball Savant without downloading pitch-level data.
        frame = statcast_pitcher_expected_stats(date.today().year, minPA=1)
        available = isinstance(frame, pd.DataFrame) and not frame.empty
    except Exception as error:
        logging.warning("Baseball Savant health check unavailable: %s", error)
        available = False
    _HEALTH_CACHE = (now, available)
    return available
