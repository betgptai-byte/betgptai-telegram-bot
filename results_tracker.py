"""Save official picks, grade final MLB games, and build result summaries."""

from __future__ import annotations

import json
import logging
import re
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from game_time import parse_game_time
from quant_engine import enrich_slate_with_quant_scores
from storage import data_file


PICKS_FILE = data_file("picks.json")
RESULTS_FILE = data_file("results.json")
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_LINESCORE_URL = "https://statsapi.mlb.com/api/v1/game/{game_id}/linescore"
MLB_GAME_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
REQUEST_TIMEOUT = 20
DIVIDER = "━━━━━━━━━━━━"
VALID_MARKET_TYPES = {
    "moneyline", "f5_moneyline", "runline", "total", "team_total", "parlay"
}
V20_SCORE_FIELDS = (
    "sp_score", "offense_score", "bullpen_score", "defense_score",
    "weather_park_score", "market_value_score", "situational_score",
)
FINAL_RESULTS = {"win", "loss", "push"}
PENDING_RESULT = "pending"
EASTERN = ZoneInfo("America/New_York")


class ResultsTrackerError(Exception):
    """A friendly error raised when tracking data cannot be read or updated."""


def _read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file and provide clear errors for missing or invalid data."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise ResultsTrackerError(f"{path.name} is missing.")
    except json.JSONDecodeError as error:
        raise ResultsTrackerError(f"{path.name} contains invalid JSON.") from error
    except OSError as error:
        raise ResultsTrackerError(f"Could not read {path.name}.") from error


def _write_json(path: Path, data: Any) -> None:
    """Write JSON through a temporary file to avoid half-written tracker data."""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as error:
        raise ResultsTrackerError(f"Could not update {path.name}.") from error


def load_picks() -> list[dict[str, Any]]:
    """Return all saved picks, creating an empty collection when needed."""
    picks = _read_json(PICKS_FILE, default=[])
    if not isinstance(picks, list):
        raise ResultsTrackerError("picks.json must contain a JSON list.")
    cleaned = [pick for pick in picks if isinstance(pick, dict)]
    changed = len(cleaned) != len(picks)
    # Older tracker entries predate market_type. Add it in memory so records
    # remain backward-compatible and are persisted on the next tracker write.
    for pick in cleaned:
        before = json.dumps(pick, sort_keys=True, default=str)
        pick["market_type"] = _market_type_for_pick(pick)
        _normalize_saved_pick(pick)
        after = json.dumps(pick, sort_keys=True, default=str)
        changed = changed or before != after
        for leg in pick.get("legs", []) if isinstance(pick.get("legs"), list) else []:
            if isinstance(leg, dict):
                leg_before = json.dumps(leg, sort_keys=True, default=str)
                leg["market_type"] = _market_type_for_pick(leg)
                _normalize_saved_pick(leg, parent=pick)
                leg_after = json.dumps(leg, sort_keys=True, default=str)
                changed = changed or leg_before != leg_after
    if changed:
        _write_json(PICKS_FILE, cleaned)
    return cleaned


def _now_iso() -> str:
    """Return a simple timestamp for pick audit fields."""
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def _pick_identity(parts: list[Any]) -> str:
    """Create a stable ID so rerunning a card does not duplicate pending picks."""
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _status_for_result(result: Any) -> str:
    """Separate pick workflow status from the MLB game status."""
    return "graded" if result in FINAL_RESULTS else PENDING_RESULT


def _is_pending_pick(pick: dict[str, Any]) -> bool:
    """Treat both modern null results and older 'pending' strings as pending."""
    if pick.get("result") in FINAL_RESULTS or pick.get("status") == "graded":
        return False
    return pick.get("status") == PENDING_RESULT or pick.get("result") in (
        None,
        "",
        PENDING_RESULT,
    )


def _official_dedupe_key(pick: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Unique official-pick key requested by BETGPTAI tracking rules."""
    return (
        str(pick.get("card_date") or pick.get("date") or ""),
        str(pick.get("game_pk") or pick.get("game_id") or ""),
        str(pick.get("market_type") or ""),
        str(pick.get("selected_team") or ""),
        str(pick.get("line") or ""),
    )


def _official_pick_id(pick: dict[str, Any]) -> str:
    """Create the public pick_id from the official dedupe key."""
    return _pick_identity(list(_official_dedupe_key(pick)))


def _game_time_et(value: Any) -> str | None:
    """Store the scheduled game time in readable Eastern Time."""
    if isinstance(value, list):
        return [_game_time_et(item) for item in value]
    parsed = parse_game_time(value)
    if parsed is None:
        return str(value) if value not in (None, "") else None
    return f"{parsed.strftime('%I:%M %p').lstrip('0')} ET"


def _opponent_for(selected_team: str, away_team: Any, home_team: Any) -> str | None:
    """Return the opponent when the selected side is one of the two teams."""
    away, home = str(away_team or ""), str(home_team or "")
    if selected_team and _teams_match(selected_team, away):
        return home
    if selected_team and _teams_match(selected_team, home):
        return away
    return None


def _selected_team_for_pick(pick: dict[str, Any]) -> str | None:
    """Read selected team from modern or older pick fields."""
    if pick.get("selected_team"):
        return str(pick["selected_team"])
    text = str(pick.get("pick_text") or pick.get("selection") or "")
    market_type = _market_type_for_pick(pick)
    if market_type == "total":
        return None
    if market_type == "team_total":
        parsed = _parse_team_total(text)
        return str(parsed["team"]) if parsed else _selection_team(text)
    if market_type == "parlay":
        return None
    return _selection_team(text)


def _normalize_saved_pick(
    pick: dict[str, Any], parent: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Upgrade older pick rows to the required tracking metadata in memory."""
    pick.setdefault("sport", parent.get("sport", "mlb") if parent else "mlb")
    if "card_date" not in pick and parent and parent.get("card_date"):
        pick["card_date"] = parent.get("card_date")
    pick.setdefault(
        "date",
        pick.get("card_date")
        or (parent.get("date") if parent else eastern_today().isoformat()),
    )
    normalized_date = normalize_pick_date(pick.get("card_date") or pick.get("date"))
    if not normalized_date:
        game_time = pick.get("game_time") or (parent or {}).get("game_time")
        parsed_game_time = parse_game_time(game_time)
        if parsed_game_time:
            normalized_date = parsed_game_time.date().isoformat()
    if not normalized_date:
        normalized_date = eastern_today().isoformat()
    if normalized_date:
        pick["date"] = normalized_date
        pick["card_date"] = normalized_date
        pick["display_date"] = display_date(normalized_date)
    if "game_pk" not in pick and "game_id" in pick:
        pick["game_pk"] = pick.get("game_id")
    if "game_id" not in pick and "game_pk" in pick:
        pick["game_id"] = pick.get("game_pk")
    if "game_time_et" not in pick:
        pick["game_time_et"] = _game_time_et(
            pick.get("game_time") or (parent or {}).get("game_time")
        )
    elif isinstance(pick.get("game_time_et"), list):
        pick["game_time_et"] = _game_time_et(pick.get("game_time_et"))
    elif isinstance(pick.get("game_time_et"), str) and "T" in str(pick.get("game_time_et")):
        pick["game_time_et"] = _game_time_et(pick.get("game_time_et"))
    if "game_time" not in pick and pick.get("game_time_et"):
        pick["game_time"] = pick["game_time_et"]
    if "pick_text" not in pick:
        pick["pick_text"] = str(pick.get("selection") or "")
    if "selection" not in pick:
        pick["selection"] = pick.get("pick_text")
    pick["market_type"] = _market_type_for_pick(pick)
    pick.setdefault("pick_type", pick["market_type"])
    selected_team = _selected_team_for_pick(pick)
    if selected_team:
        pick.setdefault("selected_team", selected_team)
    pick.setdefault(
        "opponent",
        _opponent_for(str(pick.get("selected_team") or ""), pick.get("away_team"), pick.get("home_team")),
    )
    pick.setdefault("line", _selection_point(str(pick.get("pick_text") or ""), pick["market_type"]))
    pick.setdefault("odds", _american_odds(pick.get("odds")))
    pick.setdefault(
        "source_command",
        parent.get("source_command", "unknown") if parent else "unknown",
    )
    pick.setdefault("result", None)
    pick.setdefault("status", _status_for_result(pick.get("result")))
    if "profit_units" not in pick:
        pick["profit_units"] = float(pick.get("units_won", 0) or 0)
    if "units_won" not in pick:
        pick["units_won"] = pick.get("profit_units", 0)
    pick.setdefault("units_risked", 1)
    pick.setdefault("created_at", _now_iso())
    pick.setdefault("graded_at", None)
    pick.setdefault("model_version", "BETGPTAI v20.0")
    pick.setdefault("component_scores", {})
    for field in V20_SCORE_FIELDS:
        pick.setdefault(field, None)
    pick.setdefault("final_edge_score", None)
    pick.setdefault("confidence", None)
    pick.setdefault("risk_level", None)
    pick.setdefault("data_quality_grade", None)
    pick.setdefault("pick_id", _official_pick_id(pick))
    return pick


def get_todays_hub_picks() -> dict[str, Any]:
    """Return today's saved Play of the Day and two-leg safe parlay."""
    today = eastern_today().isoformat()
    todays_picks = [pick for pick in load_picks() if _pick_card_date(pick) == today]

    # Read in reverse so a newly regenerated card wins over older history.
    play = next(
        (
            pick.get("selection")
            for pick in reversed(todays_picks)
            if pick.get("category") == "play_of_day"
        ),
        None,
    )
    parlay = next(
        (
            pick
            for pick in reversed(todays_picks)
            if pick.get("category") == "parlay"
        ),
        None,
    )

    legs: list[str] = []
    leg_details: list[dict[str, Any]] = []
    if isinstance(parlay, dict):
        saved_legs = parlay.get("legs")
        if isinstance(saved_legs, list):
            leg_details = [leg for leg in saved_legs if isinstance(leg, dict)][:2]
            legs = [
                str(leg.get("selection"))
                for leg in saved_legs
                if isinstance(leg, dict) and leg.get("selection")
            ][:2]
        # Older tracker entries may only have the combined selection string.
        if len(legs) < 2 and isinstance(parlay.get("selection"), str):
            legs = [part.strip() for part in parlay["selection"].split(" + ")][:2]

    return {
        "play_of_day": play,
        "parlay_legs": legs if len(legs) == 2 else [],
        "parlay_leg_details": leg_details if len(leg_details) == 2 else [],
    }


def get_most_recent_featured_picks(
    pick_date: str | None = None,
) -> dict[str, Any]:
    """Return featured picks from a requested date or the newest saved card."""
    picks = load_picks()
    if not picks:
        return {}

    # Dates are stored as YYYY-MM-DD, so their text values sort chronologically.
    dated_picks = [
        pick for pick in picks
        if _pick_card_date(pick)
    ]
    if not dated_picks:
        return {}
    newest_date = pick_date or max(str(_pick_card_date(pick)) for pick in dated_picks)
    newest_card = [pick for pick in dated_picks if _pick_card_date(pick) == newest_date]

    # Reverse iteration favors the latest regenerated version of the same card.
    play = next(
        (
            pick for pick in reversed(newest_card)
            if pick.get("category") == "play_of_day"
        ),
        None,
    )
    parlay = next(
        (
            pick for pick in reversed(newest_card)
            if pick.get("category") == "parlay"
        ),
        None,
    )

    legs: list[str] = []
    leg_details: list[dict[str, Any]] = []
    if isinstance(parlay, dict):
        saved_legs = parlay.get("legs")
        if isinstance(saved_legs, list):
            leg_details = [
                leg for leg in saved_legs if isinstance(leg, dict)
            ][:2]
            legs = [
                str(leg.get("selection"))
                for leg in saved_legs
                if isinstance(leg, dict) and leg.get("selection")
            ][:2]
        if len(legs) < 2 and isinstance(parlay.get("selection"), str):
            legs = [part.strip() for part in parlay["selection"].split(" + ")][:2]

    return {
        "play_of_day": play.get("selection") if isinstance(play, dict) else None,
        "line": play.get("odds") if isinstance(play, dict) else None,
        "risk_grade": play.get("risk_grade") if isinstance(play, dict) else None,
        "parlay_legs": legs if len(legs) == 2 else [],
        "play_game": {
            "game_id": play.get("game_id"),
            "away_team": play.get("away_team"),
            "home_team": play.get("home_team"),
            "game_time": play.get("game_time"),
            "status": play.get("status"),
        } if isinstance(play, dict) else {},
        "parlay_leg_details": leg_details if len(leg_details) == 2 else [],
    }


def load_results() -> dict[str, Any]:
    """Return the professional results dashboard data."""
    results = _read_json(RESULTS_FILE)
    if not isinstance(results, dict):
        raise ResultsTrackerError("results.json must contain a JSON object.")
    return results


def eastern_today() -> date:
    """Return the official BETGPTAI calendar date in Eastern Time."""
    return datetime.now(EASTERN).date()


def normalize_pick_date(value: Any) -> str | None:
    """Convert YYYY-MM-DD or MM/DD/YYYY into the saved YYYY-MM-DD format."""
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def display_date(value: Any) -> str:
    """Return MM/DD/YYYY for public dashboards."""
    normalized = normalize_pick_date(value)
    if not normalized:
        return str(value or "Unavailable")
    return datetime.strptime(normalized, "%Y-%m-%d").strftime("%m/%d/%Y")


def _normalize_team(name: str) -> str:
    """Normalize punctuation and common Athletics aliases for matching."""
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return {
        "oaklandathletics": "athletics",
        "sacramentoathletics": "athletics",
    }.get(normalized, normalized)


def _market_type_for_pick(pick: dict[str, Any]) -> str:
    """Normalize old pick_type/category values into the new market_type field."""
    market_type = str(pick.get("market_type", ""))
    if market_type.startswith("f5_"):
        return "f5_moneyline"
    if market_type in VALID_MARKET_TYPES:
        return market_type
    pick_type = str(pick.get("pick_type", ""))
    category = str(pick.get("category", ""))
    if pick_type.startswith("f5_"):
        return "f5_moneyline"
    if pick_type in VALID_MARKET_TYPES:
        return pick_type
    if category in {"f5", "f5_moneyline"}:
        return "f5_moneyline"
    if category in {"team_total", "team_totals"}:
        return "team_total"
    return "moneyline"


def _teams_match(first: str, second: str) -> bool:
    """Match full MLB names with common short forms such as 'Dodgers'."""
    first_name, second_name = _normalize_team(first), _normalize_team(second)
    if first_name == second_name:
        return True
    # Requiring four characters avoids treating very short fragments as teams.
    return min(len(first_name), len(second_name)) >= 4 and (
        first_name.endswith(second_name) or second_name.endswith(first_name)
    )


def _american_odds(value: str | int | float | None) -> int | float | None:
    """Convert a displayed American price such as +110 into a number."""
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"[+-]?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


def _pick_type(selection: str, category: str) -> str:
    """Infer the market for PLAY OF THE DAY; other sections are explicit."""
    if category in VALID_MARKET_TYPES:
        return category
    if re.search(r"(?i)\b(?:team total|TT)\b", selection):
        return "team_total"
    if re.search(r"(?i)\bF5\b", selection):
        return "f5_moneyline"
    if re.match(r"(?i)^\s*(over|under)\b", selection):
        return "total"
    if re.search(r"[+-]\d+(?:\.\d+)?(?:\s|$)", selection):
        return "runline"
    return "moneyline"


def _selection_team(selection: str) -> str:
    """Remove ML and spread notation to leave the selected team name."""
    cleaned = re.sub(r"[*_`]", "", selection).strip()
    cleaned = re.sub(r"(?i)^pick\s*:\s*", "", cleaned).strip()
    cleaned = re.sub(r"(?i)\s+ML\b", "", cleaned).strip()
    cleaned = re.sub(r"(?i)\s+F5\b", "", cleaned).strip()
    cleaned = re.sub(r"\s+[+-]\d+(?:\.\d+)?(?:\s.*)?$", "", cleaned).strip()
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip()
    return cleaned


def _selection_point(selection: str, pick_type: str) -> float | None:
    """Read a spread or total number from a displayed selection."""
    pattern = r"([+-]\d+(?:\.\d+)?)" if pick_type == "runline" else r"(?i)(?:over|under)\s+(\d+(?:\.\d+)?)"
    match = re.search(pattern, selection)
    return float(match.group(1)) if match else None


def _parse_team_total(selection: str) -> dict[str, Any] | None:
    """Parse the supported long and abbreviated team-total pick formats."""
    cleaned = re.sub(r"[*_`]", "", selection).strip()
    patterns = (
        r"^(.+?)\s+Team Total\s+(Over|Under)\s+(\d+(?:\.\d+)?)$",
        r"^(.+?)\s+TT\s+(Over|Under)\s+(\d+(?:\.\d+)?)$",
        r"^(.+?)\s+(Over|Under)\s+(\d+(?:\.\d+)?)\s+Team Total$",
    )
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            return {
                "team": match.group(1).strip(),
                "direction": match.group(2).lower(),
                "line": float(match.group(3)),
            }
    return None


def _market_key(pick_type: str) -> str:
    return {
        "moneyline": "h2h", "f5_moneyline": "f5_h2h",
        "runline": "spreads", "total": "totals", "team_total": "team_totals",
    }[pick_type]


def _quant_payload(resolved: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the v20 engine payload attached to a resolved slate game."""
    if not isinstance(resolved, dict):
        return {}
    payload = resolved.get("betgptai_quant_v20") or resolved.get("betgptai_internal")
    if isinstance(payload, dict) and isinstance(payload.get("v20"), dict):
        payload = payload["v20"]
    return payload if isinstance(payload, dict) else {}


def _average_quant_from_legs(legs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build an auditable v20 summary for a parlay from its saved legs."""
    if not legs:
        return {}
    numeric: dict[str, list[float]] = {field: [] for field in V20_SCORE_FIELDS}
    numeric["final_edge_score"] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        for field in (*V20_SCORE_FIELDS, "final_edge_score"):
            value = leg.get(field)
            if isinstance(value, (int, float)):
                numeric[field].append(float(value))
    averaged = {
        field: round(sum(values) / len(values), 2)
        for field, values in numeric.items()
        if values
    }
    if not averaged:
        return {}
    return {
        "model_version": "BETGPTAI v20.0",
        "component_scores": {
            field: averaged.get(field)
            for field in V20_SCORE_FIELDS
            if field in averaged
        },
        **averaged,
        "confidence": "Parlay",
        "risk_level": "High",
        "data_quality_grade": "Leg Average",
    }


def _apply_quant_fields(record: dict[str, Any], quant: dict[str, Any]) -> None:
    """Persist the engine context required for grading and learning."""
    components = quant.get("component_scores") if isinstance(quant.get("component_scores"), dict) else {}
    record["model_version"] = quant.get("model_version") or "BETGPTAI v20.0"
    record["component_scores"] = {
        field: quant.get(field, components.get(field))
        for field in V20_SCORE_FIELDS
        if quant.get(field, components.get(field)) is not None
    }
    for field in V20_SCORE_FIELDS:
        record[field] = quant.get(field, components.get(field))
    record["final_edge_score"] = quant.get("final_edge_score")
    record["confidence"] = quant.get("confidence")
    record["risk_level"] = quant.get("risk_level")
    record["data_quality_grade"] = quant.get("data_quality_grade")


def _pick_record(
    *,
    selected_date: str,
    sport: str,
    category: str,
    pick_type: str,
    pick_text: str,
    resolved: dict[str, Any],
    odds: int | float | None = None,
    risk_grade: int | float | None = None,
    legs: list[dict[str, Any]] | None = None,
    source_command: str = "unknown",
) -> dict[str, Any]:
    """Create the modern pick row while preserving old field aliases."""
    selected_team = None if pick_type in {"total", "parlay"} else _selected_team_for_pick({
        "pick_text": pick_text, "market_type": pick_type
    })
    line = resolved.get("line")
    if line is None and pick_type in {"runline", "total", "team_total"}:
        line = _selection_point(pick_text, pick_type)
        team_total = _parse_team_total(pick_text) if pick_type == "team_total" else None
        if team_total:
            line = team_total["line"]
    record = {
        "date": selected_date,
        "card_date": selected_date,
        "display_date": display_date(selected_date),
        "sport": sport,
        "category": category,
        "game_id": resolved.get("game_id"),
        "game_pk": resolved.get("game_id"),
        "away_team": resolved.get("away_team"),
        "home_team": resolved.get("home_team"),
        "game_time": resolved.get("game_time"),
        "game_time_et": _game_time_et(resolved.get("game_time")),
        "game_status": resolved.get("status"),
        "venue": resolved.get("venue"),
        "ballpark": resolved.get("venue"),
        "market_type": pick_type,
        "pick_type": pick_type,
        "source_command": source_command,
        "selected_team": selected_team,
        "opponent": _opponent_for(
            selected_team or "", resolved.get("away_team"), resolved.get("home_team")
        ),
        "pick_text": pick_text,
        "selection": pick_text,
        "line": line,
        "odds": odds if odds is not None else resolved.get("odds"),
        "risk_grade": risk_grade,
        "status": PENDING_RESULT,
        "result": None,
        "profit_units": 0,
        "units_risked": 1,
        "units_won": 0,
        "created_at": _now_iso(),
        "graded_at": None,
    }
    _apply_quant_fields(record, _quant_payload(resolved))
    if legs:
        parlay_quant = _average_quant_from_legs(legs)
        if parlay_quant:
            _apply_quant_fields(record, parlay_quant)
        record["legs"] = legs
        record["game_id"] = [leg.get("game_id") for leg in legs]
        record["game_pk"] = [leg.get("game_pk") or leg.get("game_id") for leg in legs]
        record["away_team"] = [leg.get("away_team") for leg in legs]
        record["home_team"] = [leg.get("home_team") for leg in legs]
        record["game_time"] = [leg.get("game_time") for leg in legs]
        record["game_time_et"] = [leg.get("game_time_et") for leg in legs]
        record["game_status"] = [leg.get("game_status") for leg in legs]
    record["pick_id"] = _official_pick_id(record)
    return record


def _resolve_pick(
    selection: str,
    pick_type: str,
    displayed_odds: int | float | None,
    slate: list[dict[str, Any]],
    context: str = "",
) -> dict[str, Any] | None:
    """Match one displayed selection back to its structured slate game."""
    point = _selection_point(selection, pick_type)
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    team_total = _parse_team_total(selection) if pick_type == "team_total" else None
    selected_team = (
        str(team_total["team"]) if team_total else _selection_team(selection)
    )
    market = _market_key(pick_type)

    # F5 leans intentionally have no derived price. Match the selected team to
    # its scheduled game, but never substitute a full-game line for an F5 line.
    if pick_type == "f5_moneyline":
        matching_games = [
            game for game in slate
            if any(
                _teams_match(str(game.get(key, "")), selected_team)
                for key in ("away_team", "home_team")
            )
        ]
        if len(matching_games) != 1:
            return None
        game = matching_games[0]
        return {
            "game_id": game.get("game_id"),
            "away_team": game.get("away_team"),
            "home_team": game.get("home_team"),
            "game_time": game.get("game_time"),
            "status": game.get("status"),
            "venue": game.get("venue"),
            "line": None,
            "odds": None,
            "betgptai_quant_v20": game.get("betgptai_quant_v20"),
            "betgptai_internal": game.get("betgptai_internal"),
        }

    for game in slate:
        if pick_type in {"moneyline", "f5_moneyline", "runline", "team_total"}:
            teams = (str(game.get("away_team", "")), str(game.get("home_team", "")))
            if not any(_teams_match(team, selected_team) for team in teams):
                continue
        for wager in game.get("best_available_prices", []):
            if wager.get("market") != market:
                continue
            if pick_type in {"moneyline", "f5_moneyline", "runline"} and not _teams_match(
                str(wager.get("outcome", "")), selected_team
            ):
                continue
            if pick_type == "total" and str(wager.get("outcome", "")).lower() not in selection.lower():
                continue
            if pick_type == "team_total":
                if team_total is None or str(wager.get("outcome", "")).lower() != team_total["direction"]:
                    continue
                wager_team = str(
                    wager.get("description") or wager.get("team") or ""
                )
                if wager_team and not _teams_match(wager_team, selected_team):
                    continue
            wager_point = wager.get("point")
            if point is not None and isinstance(wager_point, (int, float)) and abs(float(wager_point) - point) > 0.001:
                continue
            candidates.append((game, wager))

    # The displayed best price usually identifies a total when several games
    # share the same Over/Under number.
    price_matches = [
        candidate for candidate in candidates
        if displayed_odds is not None and candidate[1].get("price") == displayed_odds
    ]
    if len(price_matches) == 1:
        candidates = price_matches

    # Team names naturally included in an AI reason provide another safe tie-break.
    if len(candidates) > 1 and context:
        context_lower = context.lower()

        def mentions_game(candidate: tuple[dict[str, Any], dict[str, Any]]) -> bool:
            game = candidate[0]
            for key in ("away_team", "home_team"):
                team = str(game.get(key, ""))
                nickname = team.split()[-1] if team.split() else ""
                if team.lower() in context_lower or (
                    len(nickname) >= 4 and re.search(
                        rf"\b{re.escape(nickname.lower())}\b", context_lower
                    )
                ):
                    return True
            return False

        context_matches = [
            candidate for candidate in candidates if mentions_game(candidate)
        ]
        if len(context_matches) == 1:
            candidates = context_matches

    if len(candidates) != 1 and pick_type == "team_total" and team_total is not None:
        matching_games = [
            game for game in slate
            if any(
                _teams_match(str(game.get(key, "")), selected_team)
                for key in ("away_team", "home_team")
            )
        ]
        if len(matching_games) == 1:
            game = matching_games[0]
            return {
                "game_id": game.get("game_id"),
                "away_team": game.get("away_team"),
                "home_team": game.get("home_team"),
                "game_time": game.get("game_time"),
                "status": game.get("status"),
                "venue": game.get("venue"),
                "line": team_total["line"],
                "odds": displayed_odds,
                "betgptai_quant_v20": game.get("betgptai_quant_v20"),
                "betgptai_internal": game.get("betgptai_internal"),
            }
    if len(candidates) != 1:
        return None
    game, wager = candidates[0]
    return {
        "game_id": game.get("game_id"),
        "away_team": game.get("away_team"),
        "home_team": game.get("home_team"),
        "game_time": game.get("game_time"),
        "status": game.get("status"),
        "venue": game.get("venue"),
        "line": point,
        "odds": displayed_odds if displayed_odds is not None else wager.get("price"),
        "betgptai_quant_v20": game.get("betgptai_quant_v20"),
        "betgptai_internal": game.get("betgptai_internal"),
    }


def _section(text: str, heading: str) -> str:
    """Return the content between a heading and its next Telegram divider."""
    start = text.find(heading)
    if start < 0:
        return ""
    content_start = start + len(heading)
    end = text.find(DIVIDER, content_start)
    return text[content_start : end if end >= 0 else len(text)].strip()


def _parse_entries(section: str, featured: bool = False) -> list[dict[str, Any]]:
    """Parse Telegram pick blocks into selection, price, and surrounding text."""
    if not section:
        return []
    if featured:
        blocks = [section]
    else:
        starts = list(
            re.finditer(r"(?m)^(?:1️⃣|2️⃣|3️⃣|4️⃣|5️⃣|[1-5][.)])\s+", section)
        )
        blocks = [
            section[match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(section)].strip()
            for index, match in enumerate(starts)
        ]

    entries = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        selection = re.sub(
            r"^(?:⚾|1️⃣|2️⃣|3️⃣|4️⃣|5️⃣|[1-5][.)])\s*", "", lines[0]
        ).strip()
        selection = re.sub(r"[*_`]", "", selection)
        selection = re.sub(r"(?i)^pick\s*:\s*", "", selection).strip()
        line_value = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Line:")), None)
        risk_text = next(
            (
                line.split(":", 1)[1].strip()
                for line in lines
                if (
                    line.startswith("Risk Grade:")
                    or line.startswith("Confidence Grade:")
                    or line.startswith("🎯 Confidence Grade:")
                )
            ),
            None,
        )
        risk_match = re.search(r"\d+(?:\.\d+)?", risk_text or "")
        risk_grade: int | float | None = None
        if risk_match:
            parsed_grade = float(risk_match.group())
            risk_grade = int(parsed_grade) if parsed_grade.is_integer() else parsed_grade
        if selection and not selection.lower().startswith("no qualified"):
            entries.append({
                "selection": selection,
                "odds": _american_odds(line_value),
                "risk_grade": risk_grade,
                "context": block,
            })
    return entries


def _decimal_odds(american: int | float) -> float:
    return 1 + (american / 100 if american > 0 else 100 / abs(american))


def _combined_parlay_odds(legs: list[dict[str, Any]]) -> int | None:
    """Calculate an American parlay price from all available leg prices."""
    prices = [leg.get("odds") for leg in legs]
    if not prices or not all(isinstance(price, (int, float)) and price != 0 for price in prices):
        return None
    decimal_price = 1.0
    for price in prices:
        decimal_price *= _decimal_odds(price)
    profit = decimal_price - 1
    return round(profit * 100) if profit >= 1 else round(-100 / profit)


def extract_official_picks(
    analysis: str,
    slate: list[dict[str, Any]],
    pick_date: str | None = None,
    source_command: str = "unknown",
) -> list[dict[str, Any]]:
    """Extract the official free card and connect each pick to an MLB game."""
    selected_date = pick_date or date.today().isoformat()
    sections = (
        ("play_of_day", "🔥 PLAY OF THE DAY", True),
        ("moneyline", "🏆 TOP 2 MONEYLINE", False),
        ("moneyline", "🏆 TOP 5 MONEYLINE", False),
        ("f5_moneyline", "🔥 TOP 2 F5 MONEYLINE", False),
        ("f5_moneyline", "🔥 TOP 5 F5", False),
        ("f5_moneyline", "🔥 TOP 5 F5 MONEYLINE", False),
        ("f5_moneyline", "🔥 F5 MONEYLINE LEAN", True),
        ("runline", "📈 TOP 2 RUNLINE/SPREAD", False),
        ("runline", "📈 TOP 5 RUNLINE/SPREAD", False),
        ("runline", "📈 TOP 5 RUN LINE", False),
        ("total", "🎯 TOP 2 OVER/UNDER TOTAL RUNS", False),
        ("total", "🎯 TOP 5 OVER/UNDER TOTAL RUNS", False),
        ("total", "🎯 TOP 5 GAME TOTALS", False),
        ("team_total", "💰 TOP 2 TEAM TOTALS", False),
        ("team_total", "💰 TEAM TOTAL ANGLE", True),
        ("team_total", "💰 TOP 5 TEAM TOTALS", False),
    )
    picks: list[dict[str, Any]] = []
    for category, heading, featured in sections:
        for entry in _parse_entries(_section(analysis, heading), featured=featured):
            pick_type = _pick_type(entry["selection"], category)
            resolved = _resolve_pick(
                entry["selection"], pick_type, entry["odds"], slate, entry["context"]
            )
            if not resolved:
                continue
            picks.append(_pick_record(
                selected_date=selected_date,
                sport="mlb",
                category=category,
                pick_type=pick_type,
                pick_text=entry["selection"],
                resolved=resolved,
                odds=entry["odds"],
                risk_grade=entry["risk_grade"],
                source_command=source_command,
            ))

    # Parlay legs appear as check-mark lines and are graded independently later.
    parlay_section = _section(analysis, "🧩 2-LEG SAFE PARLAY")
    if not parlay_section:
        parlay_section = _section(analysis, "🧩 SAFE PARLAY OF THE DAY")
    if not parlay_section:
        parlay_section = _section(analysis, "🧩 TOP 3-LEG SAFE PARLAY")
    leg_selections = re.findall(r"(?m)^✅\s+(.+)$", parlay_section)[:2]
    legs = []
    for selection in leg_selections:
        pick_type = _pick_type(selection.strip(), "play_of_day")
        resolved = _resolve_pick(selection.strip(), pick_type, None, slate, parlay_section)
        if resolved:
            legs.append(_pick_record(
                selected_date=selected_date,
                sport="mlb",
                category="parlay_leg",
                pick_type=pick_type,
                pick_text=selection.strip(),
                resolved=resolved,
                odds=resolved.get("odds"),
                source_command=source_command,
            ))
    if len(legs) == 2:
        picks.append(_pick_record(
            selected_date=selected_date,
            sport="mlb",
            category="parlay",
            pick_type="parlay",
            pick_text=" + ".join(str(leg["pick_text"]) for leg in legs),
            resolved={"line": None, "odds": _combined_parlay_odds(legs)},
            odds=_combined_parlay_odds(legs),
            legs=legs,
            source_command=source_command,
        ))
    return picks


def save_official_picks(
    analysis: str,
    slate: list[dict[str, Any]],
    pick_date: str | None = None,
    source_command: str = "unknown",
) -> int:
    """Save a generated card immediately, regardless of scheduled game status."""
    existing = load_picks()
    if slate and not any(game.get("betgptai_quant_v20") for game in slate):
        try:
            slate = enrich_slate_with_quant_scores(slate, pick_date)
        except Exception:
            logging.exception("BETGPTAI v20 scoring unavailable while saving picks")
    new_picks = extract_official_picks(analysis, slate, pick_date, source_command)
    if not new_picks:
        raise ResultsTrackerError(
            "The generated free card did not contain any trackable official picks."
        )

    required_fields = {
        "pick_id", "card_date", "display_date", "sport", "source_command",
        "game_pk", "home_team", "away_team", "market_type", "pick_text",
        "game_time_et", "status", "created_at", "graded_at",
        "model_version", "component_scores", "final_edge_score", "confidence",
        "risk_level", "data_quality_grade",
    }
    for pick in new_picks:
        pick["source_command"] = source_command
        _normalize_saved_pick(pick)
        pick["pick_id"] = _official_pick_id(pick)
        missing = required_fields.difference(pick)
        if missing:
            raise ResultsTrackerError(
                "An official pick is missing: " + ", ".join(sorted(missing))
            )
        # Picks are official as soon as the card is generated. They remain
        # pending until /update_results later finds a final MLB score.
        pick["result"] = None
        pick["status"] = PENDING_RESULT
        pick["profit_units"] = 0
        pick["units_risked"] = 1
        pick["units_won"] = 0

    # Do not duplicate official picks when the same card is generated from a
    # slash command, tap menu, or image preview. The unique key is:
    # card_date + game_pk + market_type + selected_team + line.
    existing_keys = {_official_dedupe_key(pick) for pick in existing}
    saved_picks: list[dict[str, Any]] = []
    new_keys: set[tuple[str, str, str, str, str]] = set()
    for pick in new_picks:
        key = _official_dedupe_key(pick)
        if key in existing_keys or key in new_keys:
            continue
        new_keys.add(key)
        saved_picks.append(pick)
    _write_json(PICKS_FILE, existing + saved_picks)
    return len(saved_picks)


def save_soccer_picks(
    analysis: str,
    slate: list[dict[str, Any]],
    pick_date: str | None = None,
    category: str = "soccer",
) -> int:
    """Save delivered soccer card picks as pending tracking rows.

    Soccer grading is intentionally not performed by the MLB grader, but saving
    the rows keeps the official card audit complete across public and premium
    commands.
    """
    selected_date = pick_date or date.today().isoformat()
    if not analysis.strip():
        return 0
    match_lookup = {
        str(game.get("home_team", "")): game for game in slate if isinstance(game, dict)
    }
    match_lookup.update({
        str(game.get("away_team", "")): game for game in slate if isinstance(game, dict)
    })
    entries = []
    sections = (
        _parse_entries(_section(analysis, "🔥 PLAY OF THE DAY"), featured=True)
        + _parse_entries(_section(analysis, "🏆 TOP 2 SOCCER PLAYS"))
    )
    if not sections:
        # Owner cards vary by market; keep the first few non-footer lines as an audit.
        lines = [
            re.sub(r"^[✅⚽🎯🔥🏆\\d️⃣.\\s]+", "", line).strip()
            for line in analysis.splitlines()
            if line.strip() and not line.startswith(DIVIDER)
        ]
        sections = [
            {"selection": line, "odds": None, "risk_grade": None, "context": line}
            for line in lines[:3]
            if line and "Educational analysis" not in line
        ]
    for entry in sections[:5]:
        pick_text = str(entry.get("selection") or "").strip()
        if not pick_text:
            continue
        matched_game = next(
            (
                game for team, game in match_lookup.items()
                if team and team.lower() in str(entry.get("context", pick_text)).lower()
            ),
            slate[0] if slate else {},
        )
        resolved = {
            "game_id": matched_game.get("match_id"),
            "away_team": matched_game.get("away_team"),
            "home_team": matched_game.get("home_team"),
            "game_time": matched_game.get("kickoff"),
            "status": matched_game.get("status"),
            "line": None,
            "odds": entry.get("odds"),
        }
        entries.append(_pick_record(
            selected_date=selected_date,
            sport="soccer",
            category=category,
            pick_type="moneyline",
            pick_text=pick_text,
            resolved=resolved,
            odds=entry.get("odds"),
            risk_grade=entry.get("risk_grade"),
        ))
    if not entries:
        return 0
    existing = load_picks()
    entry_ids = {entry["pick_id"] for entry in entries}
    preserved = [
        pick for pick in existing
        if not (
            pick.get("sport") == "soccer"
            and _pick_card_date(pick) == selected_date
            and pick.get("category") == category
            and _is_pending_pick(pick)
            and pick.get("pick_id") in entry_ids
        )
    ]
    _write_json(PICKS_FILE, preserved + entries)
    return len(entries)


def debug_picks_summary(target_date: str | None = None) -> dict[str, Any]:
    """Owner-only diagnostics for saved pick metadata and grading issues."""
    selected_date = target_date or eastern_today().isoformat()
    all_picks = load_picks()
    picks = [pick for pick in all_picks if _pick_card_date(pick) == selected_date]
    errors = []
    try:
        payload = _read_json(data_file("grading_errors.json"), default={})
        if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
            errors.extend(str(item) for item in payload["errors"][-5:])
    except ResultsTrackerError:
        pass
    pick_errors = [
        str(pick.get("last_grading_error"))
        for pick in picks
        if pick.get("last_grading_error")
    ][-5:]
    errors.extend(pick_errors)
    return {
        "total_today": len(picks),
        "pending": sum(_is_pending_pick(pick) for pick in picks),
        "graded": sum(pick.get("result") in FINAL_RESULTS for pick in picks),
        "missing_game_id": sum(not (pick.get("game_pk") or pick.get("game_id")) for pick in picks),
        "missing_market_type": sum(not pick.get("market_type") for pick in picks),
        "missing_selected_team": sum(
            pick.get("market_type") not in {"total", "parlay"}
            and not pick.get("selected_team")
            for pick in picks
        ),
        "missing_card_date": sum(not pick.get("card_date") for pick in all_picks),
        "last_errors": errors[-5:],
    }


def _fetch_f5_score(game_id: int) -> dict[str, int] | None:
    """Return the official runs through five innings when MLB supplies them."""
    try:
        response = requests.get(
            MLB_LINESCORE_URL.format(game_id=game_id),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        logging.warning("F5 linescore unavailable for MLB game %s", game_id)
        return None

    innings = payload.get("innings", [])
    first_five = [
        inning for inning in innings
        if isinstance(inning, dict) and inning.get("num") in {1, 2, 3, 4, 5}
    ]
    if len(first_five) < 5:
        return None
    away_runs = sum(
        inning.get("away", {}).get("runs", 0) or 0 for inning in first_five
    )
    home_runs = sum(
        inning.get("home", {}).get("runs", 0) or 0 for inning in first_five
    )
    if not isinstance(away_runs, int) or not isinstance(home_runs, int):
        return None
    return {"f5_away_score": away_runs, "f5_home_score": home_runs}


def _coerce_game_pk(value: Any) -> int | None:
    """Return a numeric MLB game_pk from modern or older saved pick fields."""
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _final_game_from_feed(game_id: int, include_f5: bool = False) -> dict[str, Any] | None:
    """Fetch one exact MLB game by game_pk and return final score metadata.

    This is intentionally used as a second source after the schedule endpoint so
    a saved official pick never stays pending when its exact game_pk is final.
    """
    try:
        response = requests.get(
            MLB_GAME_FEED_URL.format(game_id=game_id),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        logging.warning("MLB game feed unavailable for game_pk %s", game_id)
        return None

    status = payload.get("gameData", {}).get("status", {})
    if status.get("abstractGameState") != "Final":
        return None
    teams = payload.get("gameData", {}).get("teams", {})
    linescore = payload.get("liveData", {}).get("linescore", {})
    away_score = linescore.get("teams", {}).get("away", {}).get("runs")
    home_score = linescore.get("teams", {}).get("home", {}).get("runs")
    if not isinstance(away_score, int) or not isinstance(home_score, int):
        return None
    final_game = {
        "game_id": game_id,
        "game_pk": game_id,
        "status": status.get("detailedState") or status.get("abstractGameState"),
        "away_team": teams.get("away", {}).get("name"),
        "home_team": teams.get("home", {}).get("name"),
        "away_score": away_score,
        "home_score": home_score,
    }
    if include_f5:
        f5_score = _fetch_f5_score(game_id)
        if f5_score:
            final_game.update(f5_score)
    return final_game


def _fetch_final_games(
    game_date: str, f5_game_ids: set[int] | None = None
) -> dict[int, dict[str, Any]]:
    """Fetch final MLB scores for one pick date."""
    try:
        response = requests.get(
            MLB_SCHEDULE_URL,
            params={"sportId": "1", "date": game_date},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ResultsTrackerError(f"Could not load MLB scores for {game_date}.") from error

    finals: dict[int, dict[str, Any]] = {}
    for group in payload.get("dates", []):
        for game in group.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            away = game.get("teams", {}).get("away", {})
            home = game.get("teams", {}).get("home", {})
            away_score, home_score = away.get("score"), home.get("score")
            game_id = game.get("gamePk")
            if isinstance(game_id, int) and isinstance(away_score, int) and isinstance(home_score, int):
                final_game = {
                    "game_id": game_id,
                    "game_pk": game_id,
                    "away_team": away.get("team", {}).get("name"),
                    "home_team": home.get("team", {}).get("name"),
                    "away_score": away_score, "home_score": home_score,
                }
                if f5_game_ids and game_id in f5_game_ids:
                    f5_score = _fetch_f5_score(game_id)
                    if f5_score:
                        final_game.update(f5_score)
                finals[game_id] = final_game
    return finals


def _fetch_final_games_by_pk(
    game_ids: set[int], f5_game_ids: set[int] | None = None
) -> dict[int, dict[str, Any]]:
    """Fetch exact saved MLB games by game_pk."""
    finals: dict[int, dict[str, Any]] = {}
    for game_id in sorted(game_ids):
        final_game = _final_game_from_feed(
            game_id,
            include_f5=bool(f5_game_ids and game_id in f5_game_ids),
        )
        if final_game:
            finals[game_id] = final_game
    return finals


def _debug_game_by_pk(game_id: int, include_f5: bool = False) -> dict[str, Any]:
    """Return a best-effort debug snapshot for one MLB game_pk."""
    snapshot: dict[str, Any] = {
        "game_pk": game_id,
        "api_status": "unavailable",
        "final_score": "Unavailable",
    }
    try:
        response = requests.get(
            MLB_GAME_FEED_URL.format(game_id=game_id),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        snapshot["error"] = str(error)
        return snapshot
    status = payload.get("gameData", {}).get("status", {})
    teams = payload.get("gameData", {}).get("teams", {})
    linescore = payload.get("liveData", {}).get("linescore", {})
    away_team = teams.get("away", {}).get("name")
    home_team = teams.get("home", {}).get("name")
    away_score = linescore.get("teams", {}).get("away", {}).get("runs")
    home_score = linescore.get("teams", {}).get("home", {}).get("runs")
    snapshot.update({
        "api_status": status.get("detailedState") or status.get("abstractGameState") or "Unknown",
        "away_team": away_team,
        "home_team": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "final_score": (
            f"{away_team} {away_score} - {home_team} {home_score}"
            if isinstance(away_score, int) and isinstance(home_score, int)
            else "Unavailable"
        ),
    })
    if include_f5:
        snapshot.update(_fetch_f5_score(game_id) or {})
    return snapshot


def _find_final_game(
    pick: dict[str, Any], final_games: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the official final game by game_pk, then team/date fallback."""
    game_id = pick.get("game_pk", pick.get("game_id"))
    if isinstance(game_id, int) and game_id in final_games:
        return final_games[game_id]
    try:
        numeric_id = int(str(game_id))
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None and numeric_id in final_games:
        return final_games[numeric_id]
    away = str(pick.get("away_team") or "")
    home = str(pick.get("home_team") or "")
    if not away or not home:
        return None
    for game in final_games.values():
        if _teams_match(away, str(game.get("away_team", ""))) and _teams_match(
            home, str(game.get("home_team", ""))
        ):
            return game
    return None


def grade_debug_report(target_date: str | None = None) -> str:
    """Owner-only grading diagnostic for a specific card date."""
    selected_date = normalize_pick_date(target_date) or eastern_today().isoformat()
    picks = [
        pick for pick in load_picks()
        if pick.get("sport", "mlb") == "mlb"
        and pick.get("category") != "parlay_leg"
        and _pick_card_date(pick) == selected_date
    ]
    if not picks:
        return f"🧪 MLB GRADE DEBUG\n\nDate: {display_date(selected_date)}\nPicks found: 0"

    game_ids: set[int] = set()
    f5_ids: set[int] = set()
    for pick in picks:
        game_id = _coerce_game_pk(pick.get("game_pk", pick.get("game_id")))
        if game_id:
            game_ids.add(game_id)
            if _market_type_for_pick(pick) == "f5_moneyline":
                f5_ids.add(game_id)
        for leg in pick.get("legs", []) if isinstance(pick.get("legs"), list) else []:
            if not isinstance(leg, dict):
                continue
            leg_game_id = _coerce_game_pk(leg.get("game_pk", leg.get("game_id")))
            if leg_game_id:
                game_ids.add(leg_game_id)
                if _market_type_for_pick(leg) == "f5_moneyline":
                    f5_ids.add(leg_game_id)
    finals = _fetch_final_games_by_pk(game_ids, f5_ids)
    snapshots = {
        game_id: _debug_game_by_pk(game_id, game_id in f5_ids)
        for game_id in sorted(game_ids)
    }
    lines = [
        "🧪 MLB GRADE DEBUG",
        f"Date: {display_date(selected_date)}",
        f"Picks found: {len(picks)}",
        "",
    ]
    for index, pick in enumerate(picks, 1):
        market_type = _market_type_for_pick(pick)
        game_id = _coerce_game_pk(pick.get("game_pk", pick.get("game_id")))
        result = (
            _grade_parlay(pick, {selected_date: finals})
            if market_type == "parlay"
            else _grade_single(pick, finals)
        )
        snapshot = snapshots.get(game_id or 0, {})
        lines.extend([
            f"{index}. {_pending_pick_label(pick)}",
            f"game_pk: {game_id or 'Missing'}",
            f"MLB API status: {snapshot.get('api_status', 'Unavailable')}",
            f"Final score: {snapshot.get('final_score', 'Unavailable')}",
            f"market_type: {market_type}",
            f"grading decision: {result}",
        ])
        if pick.get("last_grading_error"):
            lines.append(f"error: {pick['last_grading_error']}")
        if market_type == "parlay" and isinstance(pick.get("legs"), list):
            for leg_index, leg in enumerate(pick["legs"], 1):
                if not isinstance(leg, dict):
                    continue
                leg_game_id = _coerce_game_pk(leg.get("game_pk", leg.get("game_id")))
                leg_snapshot = snapshots.get(leg_game_id or 0, {})
                leg_result = _grade_single(leg, finals)
                lines.extend([
                    f"  Leg {leg_index}: {_pending_pick_label(leg)}",
                    f"  leg_game_pk: {leg_game_id or 'Missing'}",
                    f"  leg_status: {leg_snapshot.get('api_status', 'Unavailable')}",
                    f"  leg_score: {leg_snapshot.get('final_score', 'Unavailable')}",
                    f"  leg_decision: {leg_result}",
                ])
        lines.append("")
    return "\n".join(lines).strip()


def _team_scores(
    selection: str,
    game: dict[str, Any],
    away_score_key: str = "away_score",
    home_score_key: str = "home_score",
) -> tuple[int, int] | None:
    team_total = _parse_team_total(selection)
    selected_team = (
        str(team_total["team"]) if team_total else selection
    )
    away_score, home_score = game.get(away_score_key), game.get(home_score_key)
    if not isinstance(away_score, int) or not isinstance(home_score, int):
        return None
    if _teams_match(selected_team, str(game["away_team"])):
        return away_score, home_score
    if _teams_match(selected_team, str(game["home_team"])):
        return home_score, away_score
    return None


def _grade_single(pick: dict[str, Any], final_games: dict[int, dict[str, Any]]) -> str:
    """Grade one moneyline, spread, or total selection."""
    game = _find_final_game(pick, final_games)
    if not game:
        return PENDING_RESULT
    pick_type = _market_type_for_pick(pick)
    selection = str(
        pick.get("selected_team") or pick.get("pick_text") or pick.get("selection") or ""
    )
    pick_text = str(pick.get("pick_text") or pick.get("selection") or selection)
    if pick_type == "moneyline":
        scores = _team_scores(selection, game)
        if not scores:
            return PENDING_RESULT
        selected_score, opponent_score = scores
        return "win" if selected_score > opponent_score else "loss"
    if pick_type == "f5_moneyline":
        scores = _team_scores(
            selection, game, "f5_away_score", "f5_home_score"
        )
        if not scores:
            return PENDING_RESULT
        selected_score, opponent_score = scores
        if selected_score == opponent_score:
            return "push"
        return "win" if selected_score > opponent_score else "loss"
    if pick_type == "runline":
        scores = _team_scores(selection, game)
        spread = pick.get("line") if isinstance(pick.get("line"), (int, float)) else _selection_point(pick_text, "runline")
        if not scores or spread is None:
            return PENDING_RESULT
        adjusted_score = scores[0] + spread
        return "win" if adjusted_score > scores[1] else "loss" if adjusted_score < scores[1] else "push"
    if pick_type == "total":
        total_line = pick.get("line") if isinstance(pick.get("line"), (int, float)) else _selection_point(pick_text, "total")
        if total_line is None:
            return PENDING_RESULT
        final_total = game["away_score"] + game["home_score"]
        if final_total == total_line:
            return "push"
        wants_over = pick_text.lower().startswith("over")
        return "win" if (final_total > total_line) == wants_over else "loss"
    if pick_type == "team_total":
        team_total = _parse_team_total(pick_text)
        scores = _team_scores(str(pick.get("selected_team") or pick_text), game)
        if not team_total or not scores:
            return PENDING_RESULT
        selected_runs = scores[0]
        line = pick.get("line") if isinstance(pick.get("line"), (int, float)) else team_total["line"]
        if selected_runs == line:
            return "push"
        wants_over = team_total["direction"] == "over"
        return "win" if (selected_runs > line) == wants_over else "loss"
    return PENDING_RESULT


def _profit_for_result(result: str, odds: Any, units_risked: Any) -> float:
    risk = float(units_risked) if isinstance(units_risked, (int, float)) else 1.0
    if result == "loss":
        return -risk
    if result != "win" or not isinstance(odds, (int, float)) or odds == 0:
        return 0.0
    return round(risk * (odds / 100 if odds > 0 else 100 / abs(odds)), 2)


def _grade_parlay(pick: dict[str, Any], finals_by_date: dict[str, dict[int, dict[str, Any]]]) -> str:
    legs = pick.get("legs", [])
    if not isinstance(legs, list) or not legs:
        return "pending"
    final_games = finals_by_date.get(str(_pick_card_date(pick)), {})
    results = [_grade_single(leg, final_games) for leg in legs]
    for leg, result in zip(legs, results):
        leg["result"] = result
        leg["status"] = _status_for_result(result)
        if result in FINAL_RESULTS:
            leg["graded_at"] = _now_iso()
            leg["profit_units"] = _profit_for_result(
                result, leg.get("odds"), leg.get("units_risked", 1)
            )
            leg["units_won"] = leg["profit_units"]
    if "loss" in results:
        return "loss"
    if PENDING_RESULT in results:
        return PENDING_RESULT
    if "push" in results:
        return "push"
    return "win" if all(result == "win" for result in results) else "pending"


def _pick_date(value: Any) -> date | None:
    normalized = normalize_pick_date(value)
    if not normalized:
        return None
    return datetime.strptime(normalized, "%Y-%m-%d").date()


def _pick_card_date(pick: dict[str, Any]) -> str | None:
    """Return the canonical card date for filtering and grading."""
    return normalize_pick_date(pick.get("card_date") or pick.get("date"))


def _summary(picks: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(pick.get("result") == "win" for pick in picks)
    losses = sum(pick.get("result") == "loss" for pick in picks)
    pushes = sum(pick.get("result") == "push" for pick in picks)
    pending = sum(_is_pending_pick(pick) for pick in picks)
    graded = wins + losses + pushes
    decided = wins + losses
    return {
        "wins": wins, "losses": losses, "pushes": pushes,
        "win_percentage": round(wins / decided * 100, 1) if decided else 0.0,
        "profit_units": round(sum(float(
            pick.get("profit_units", pick.get("units_won", 0)) or 0
        ) for pick in picks), 2),
        "pending": pending,
        "graded": graded,
    }


MARKET_RESULT_GROUPS = {
    "moneyline": "moneyline",
    "f5_moneyline": "f5_moneyline",
    "runline": "runline",
    "totals": "total",
    "team_totals": "team_total",
    "parlays": "parlay",
}


def _summaries_for_picks(picks: list[dict[str, Any]]) -> dict[str, Any]:
    """Build overall and market-specific summaries for a pick collection."""
    return {
        "overall": _summary(picks),
        **{
            result_key: _summary([
                pick for pick in picks
                if _market_type_for_pick(pick) == market_type
            ])
            for result_key, market_type in MARKET_RESULT_GROUPS.items()
        },
    }


def _filter_mlb_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only official MLB picks, including parlays, from the tracker."""
    return [
        pick for pick in picks
        if pick.get("sport", "mlb") == "mlb"
        and pick.get("category") != "parlay_leg"
    ]


def rebuild_results(picks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recalculate daily, rolling, and season dashboards from picks.json."""
    all_picks = _filter_mlb_picks(picks if picks is not None else load_picks())
    graded_picks = [pick for pick in all_picks if pick.get("result") in FINAL_RESULTS]
    today = eastern_today()
    dated = [(pick, _pick_date(_pick_card_date(pick))) for pick in graded_picks]

    daily: dict[str, Any] = {}
    for pick, pick_day in dated:
        if not pick_day:
            continue
        key = pick_day.strftime("%m/%d/%Y")
        daily.setdefault(key, []).append(pick)

    week_picks = [
        pick for pick, pick_day in dated
        if pick_day and today - timedelta(days=6) <= pick_day <= today
    ]
    month_picks = [
        pick for pick, pick_day in dated
        if pick_day and today - timedelta(days=29) <= pick_day <= today
    ]
    season_picks = [
        pick for pick, pick_day in dated
        if pick_day and pick_day.year == today.year
    ]
    results = {
        "daily": {
            day: _summaries_for_picks(day_picks)
            for day, day_picks in sorted(daily.items())
        },
        "last_7_days": _summaries_for_picks(week_picks),
        "last_30_days": _summaries_for_picks(month_picks),
        "season": _summaries_for_picks(season_picks),
        "last_updated": datetime.now(EASTERN).strftime("%m/%d/%Y %I:%M %p ET"),
    }
    _write_json(RESULTS_FILE, results)
    return results


def _record_line(summary: dict[str, Any]) -> str:
    """Format one W-L-P / Win% / units block."""
    return (
        f"W-L-P: {summary.get('wins', 0)}-{summary.get('losses', 0)}-"
        f"{summary.get('pushes', 0)}\n"
        f"Win %: {summary.get('win_percentage', 0):g}%\n"
        f"Profit Units: {summary.get('profit_units', 0):+g}"
    )


def _market_short(summary: dict[str, Any]) -> str:
    """Compact market row for daily results."""
    return (
        f"{summary.get('wins', 0)}-{summary.get('losses', 0)}-"
        f"{summary.get('pushes', 0)} "
        f"({summary.get('profit_units', 0):+g}u)"
    )


def _picks_for_date(picks: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    """Filter official MLB picks for one saved YYYY-MM-DD date."""
    normalized = normalize_pick_date(target_date)
    if not normalized:
        return []
    return [
        pick for pick in _filter_mlb_picks(picks)
        if _pick_card_date(pick) == normalized
    ]


def _all_picks_for_date(
    picks: list[dict[str, Any]], target_date: str
) -> list[dict[str, Any]]:
    """Filter all official picks for one saved YYYY-MM-DD card date."""
    normalized = normalize_pick_date(target_date)
    if not normalized:
        return []
    return [
        pick for pick in picks
        if pick.get("category") != "parlay_leg"
        and _pick_card_date(pick) == normalized
    ]


def _pending_pick_label(pick: dict[str, Any]) -> str:
    """Human-readable one-line label for pending official picks."""
    return str(pick.get("pick_text") or pick.get("selection") or "Official pick")


def _daily_wlp_line(picks: list[dict[str, Any]]) -> str:
    """Return W-L-P from graded picks only."""
    wins = sum(pick.get("result") == "win" for pick in picks)
    losses = sum(pick.get("result") == "loss" for pick in picks)
    pushes = sum(pick.get("result") == "push" for pick in picks)
    return f"{wins}-{losses}-{pushes}"


def _daily_pending_count(picks: list[dict[str, Any]]) -> int:
    """Count saved picks that are still waiting to be graded."""
    return sum(_is_pending_pick(pick) for pick in picks)


def build_daily_results_dashboard(target_date: str | None = None) -> str:
    """Render the default daily results card from picks.json."""
    selected_date = normalize_pick_date(target_date) or eastern_today().isoformat()
    picks = _all_picks_for_date(load_picks(), selected_date)
    if not picks:
        return f"No official picks saved for {display_date(selected_date)} yet."

    mlb_picks = [pick for pick in picks if pick.get("sport", "mlb") == "mlb"]
    soccer_picks = [pick for pick in picks if pick.get("sport") == "soccer"]
    pending = [pick for pick in picks if _is_pending_pick(pick)]
    pending_rows = (
        "\n".join(f"- {_pending_pick_label(pick)}" for pick in pending[:30])
        if pending
        else "None"
    )
    return (
        "📊 BETGPTAI DAILY RESULTS\n"
        f"📅 Date: {display_date(selected_date)}\n\n"
        "Overall:\n"
        f"W-L-P: {_daily_wlp_line(picks)}\n"
        f"Pending: {_daily_pending_count(picks)}\n\n"
        "⚾ MLB:\n"
        f"W-L-P: {_daily_wlp_line(mlb_picks)}\n"
        f"Pending: {_daily_pending_count(mlb_picks)}\n\n"
        "⚽ Soccer:\n"
        f"W-L-P: {_daily_wlp_line(soccer_picks)}\n"
        f"Pending: {_daily_pending_count(soccer_picks)}\n\n"
        "Pending Picks:\n"
        f"{pending_rows}\n\n"
        f"Last Updated: {datetime.now(EASTERN).strftime('%m/%d/%Y %I:%M %p ET')}"
    )


def available_card_dates() -> list[str]:
    """Return saved card_date values from picks.json as YYYY-MM-DD."""
    dates = {
        card_date for pick in load_picks()
        if (card_date := _pick_card_date(pick))
    }
    return sorted(dates)


def missing_results_message(target_date: str) -> str:
    """Explain that a requested date has no picks and list available card dates."""
    selected_date = normalize_pick_date(target_date) or target_date
    dates = available_card_dates()
    if not dates:
        return (
            f"No official picks saved for {display_date(selected_date)}.\n\n"
            "No saved card dates were found in picks.json."
        )
    rows = "\n".join(f"- {display_date(day)}" for day in dates)
    return (
        f"No official picks saved for {display_date(selected_date)}.\n\n"
        "Available card dates:\n"
        f"{rows}\n\n"
        "Tap one below:"
    )


def build_range_results_dashboard(days: int | None = None) -> str:
    """Render 7-day, 30-day, or season results from picks.json."""
    picks = [
        pick for pick in _filter_mlb_picks(load_picks())
        if pick.get("result") in FINAL_RESULTS
    ]
    today = eastern_today()
    if days is None:
        label = "SEASON"
        scoped = [
            pick for pick in picks
            if (pick_day := _pick_date(_pick_card_date(pick))) and pick_day.year == today.year
        ]
    else:
        label = f"LAST {days} DAYS"
        start = today - timedelta(days=days - 1)
        scoped = [
            pick for pick in picks
            if (pick_day := _pick_date(_pick_card_date(pick))) and start <= pick_day <= today
        ]
    summaries = _summaries_for_picks(scoped)
    overall = summaries["overall"]
    return (
        f"📊 BETGPTAI {label} RESULTS\n\n"
        f"Overall:\n{_record_line(overall)}\n\n"
        f"Moneyline:\n{_record_line(summaries['moneyline'])}\n\n"
        f"F5 Moneyline:\n{_record_line(summaries['f5_moneyline'])}\n\n"
        f"Runline:\n{_record_line(summaries['runline'])}\n\n"
        f"Totals:\n{_record_line(summaries['totals'])}\n\n"
        f"Team Totals:\n{_record_line(summaries['team_totals'])}\n\n"
        f"Parlays:\n{_record_line(summaries['parlays'])}\n\n"
        f"Last Updated: {datetime.now(EASTERN).strftime('%m/%d/%Y %I:%M %p ET')}"
    )


def debug_results_summary() -> dict[str, Any]:
    """Owner-only results diagnostics from picks.json."""
    picks = load_picks()
    today = eastern_today().isoformat()
    today_picks = _picks_for_date(picks, today)
    dates = sorted({
        display_date(_pick_card_date(pick)) for pick in picks
        if _pick_card_date(pick)
    })
    last_10 = [
        {
            "date": display_date(_pick_card_date(pick)),
            "card_date": pick.get("card_date"),
            "status": pick.get("status"),
            "result": pick.get("result"),
            "pick_text": pick.get("pick_text") or pick.get("selection"),
        }
        for pick in picks[-10:]
    ]
    return {
        "total_picks": len(picks),
        "picks_today": len(today_picks),
        "graded_today": sum(pick.get("result") in FINAL_RESULTS for pick in today_picks),
        "pending_today": sum(_is_pending_pick(pick) for pick in today_picks),
        "missing_card_date": sum(not pick.get("card_date") for pick in picks),
        "missing_game_pk": sum(
            pick.get("sport", "mlb") == "mlb"
            and pick.get("category") != "parlay_leg"
            and not (pick.get("game_pk") or pick.get("game_id"))
            for pick in picks
        ),
        "missing_market_type": sum(not pick.get("market_type") for pick in picks),
        "dates": dates,
        "last_10": last_10,
    }


def update_results_from_mlb() -> dict[str, int]:
    """Grade only pending picks, then update picks.json and results.json once."""
    return grade_mlb_picks_for_date(None)


def grade_mlb_picks_for_date(target_date: str | None = None) -> dict[str, int]:
    """Grade saved MLB picks for one date, or all pending MLB dates."""
    normalized_target = normalize_pick_date(target_date) if target_date else None
    picks = load_picks()

    # An empty tracker needs no MLB request and nothing should be graded.
    if not picks:
        return {
            "newly_graded": 0,
            "pending": 0,
            "graded": 0,
            "total_picks": 0,
            "missing_metadata": 0,
            "errors": 0,
        }

    pending_dates = {
        str(_pick_card_date(pick)) for pick in picks
        if pick.get("sport", "mlb") == "mlb"
        and _is_pending_pick(pick)
        and _pick_card_date(pick)
        and (normalized_target is None or _pick_card_date(pick) == normalized_target)
    }
    f5_games_by_date: dict[str, set[int]] = {}
    game_ids_by_date: dict[str, set[int]] = {}
    errors: list[str] = []
    for pick in picks:
        if (
            pick.get("sport", "mlb") == "mlb"
            and _is_pending_pick(pick)
            and (normalized_target is None or _pick_card_date(pick) == normalized_target)
        ):
            pick_date = str(_pick_card_date(pick))
            game_id = _coerce_game_pk(pick.get("game_pk", pick.get("game_id")))
            if game_id:
                game_ids_by_date.setdefault(pick_date, set()).add(game_id)
                if _market_type_for_pick(pick) == "f5_moneyline":
                    f5_games_by_date.setdefault(pick_date, set()).add(game_id)
            for leg in pick.get("legs", []) if isinstance(pick.get("legs"), list) else []:
                if not isinstance(leg, dict):
                    continue
                leg_game_id = _coerce_game_pk(leg.get("game_pk", leg.get("game_id")))
                if leg_game_id:
                    game_ids_by_date.setdefault(pick_date, set()).add(leg_game_id)
                    if _market_type_for_pick(leg) == "f5_moneyline":
                        f5_games_by_date.setdefault(pick_date, set()).add(leg_game_id)
    finals_by_date = {}
    for game_date in pending_dates:
        finals_by_date[game_date] = {}
        try:
            finals_by_date[game_date] = _fetch_final_games(
                game_date, f5_games_by_date.get(game_date, set())
            )
        except ResultsTrackerError as error:
            errors.append(str(error))
        # Exact game_pk fetch is the safety net for saved official picks. Run it
        # even if the date schedule endpoint failed.
        finals_by_date[game_date].update(
            _fetch_final_games_by_pk(
                game_ids_by_date.get(game_date, set()),
                f5_games_by_date.get(game_date, set()),
            )
        )
    newly_graded = 0
    missing_metadata = 0
    for pick in picks:
        # This guard prevents any previously settled pick from being counted twice.
        if pick.get("sport", "mlb") != "mlb" or not _is_pending_pick(pick):
            continue
        if normalized_target is not None and _pick_card_date(pick) != normalized_target:
            continue
        pick["market_type"] = _market_type_for_pick(pick)
        _normalize_saved_pick(pick)
        missing_required = [
            key for key in ("date", "market_type", "pick_text")
            if not pick.get(key)
        ]
        if pick["market_type"] != "parlay":
            if not (pick.get("game_pk") or pick.get("game_id")):
                missing_required.append("game_id")
            if pick["market_type"] not in {"total"} and not pick.get("selected_team"):
                missing_required.append("selected_team")
        if missing_required:
            missing_metadata += 1
            pick["last_grading_error"] = "Missing metadata: " + ", ".join(sorted(set(missing_required)))
            continue
        if pick.get("market_type") == "parlay":
            result = _grade_parlay(pick, finals_by_date)
        else:
            pick["pick_type"] = pick["market_type"]
            result = _grade_single(pick, finals_by_date.get(str(_pick_card_date(pick)), {}))
        if result != PENDING_RESULT:
            pick["result"] = result
            pick["status"] = _status_for_result(result)
            pick["graded_at"] = _now_iso()
            pick["profit_units"] = _profit_for_result(
                result, pick.get("odds"), pick.get("units_risked", 1)
            )
            pick["units_won"] = pick["profit_units"]
            pick.pop("last_grading_error", None)
            newly_graded += 1

    _write_json(PICKS_FILE, picks)
    rebuild_results(picks)
    scoped_picks = [
        pick for pick in picks
        if pick.get("sport", "mlb") == "mlb"
        and (normalized_target is None or _pick_card_date(pick) == normalized_target)
    ]
    pending_count = sum(_is_pending_pick(pick) for pick in scoped_picks)
    graded_count = sum(
        pick.get("result") in FINAL_RESULTS for pick in scoped_picks
    )
    if errors:
        _write_json(data_file("grading_errors.json"), {
            "updated_at": _now_iso(),
            "errors": errors[-20:],
        })
    return {
        "newly_graded": newly_graded,
        "pending": pending_count,
        "graded": graded_count,
        "total_picks": len(scoped_picks),
        "missing_metadata": missing_metadata,
        "errors": len(errors),
    }
