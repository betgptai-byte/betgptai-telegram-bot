"""BETGPTAI data verification engine.

This module does not change betting logic. It enriches admin/research surfaces
with transparent source/confidence metadata so MLB Stats API remains the source
of truth while ESPN can verify or fill context gaps.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from api import espn_client
from storage import data_file


MLB_SOURCE = "MLB Stats API"
ESPN_SOURCE = "ESPN"


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _present(value: Any) -> bool:
    return value not in (None, "", "unavailable", "Unavailable", "N/A", [], {})


def _norm(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _values_agree(left: Any, right: Any) -> bool:
    if not _present(left) or not _present(right):
        return False
    return _norm(left) == _norm(right)


def _log_verification_issue(issue: dict[str, Any]) -> None:
    log_path = data_file("logs") / "api.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _timestamp(),
        "component": "verification_engine",
        **issue,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def verified_field(
    *,
    name: str,
    primary_value: Any,
    primary_source: str = MLB_SOURCE,
    fallback_value: Any = None,
    fallback_source: str = ESPN_SOURCE,
    compare: bool = True,
) -> dict[str, Any]:
    """Return a field verification envelope.

    If MLB and ESPN disagree, MLB's value is kept, confidence is reduced, and an
    admin-only log issue is written. ESPN never overwrites MLB automatically.
    """
    primary_ok = _present(primary_value)
    fallback_ok = _present(fallback_value)
    disagreement = False
    if primary_ok and fallback_ok and compare and not _values_agree(primary_value, fallback_value):
        disagreement = True
        value = primary_value
        source = primary_source
        confidence = 55
        verified = False
        _log_verification_issue({
            "event": "source_disagreement",
            "field": name,
            "primary_source": primary_source,
            "primary_value": primary_value,
            "fallback_source": fallback_source,
            "fallback_value": fallback_value,
            "recovery": "kept_primary_lowered_confidence",
        })
    elif primary_ok and fallback_ok:
        value = primary_value
        source = f"{primary_source}+{fallback_source}"
        confidence = 98
        verified = True
    elif primary_ok:
        value = primary_value
        source = primary_source
        confidence = 88
        verified = True
    elif fallback_ok:
        value = fallback_value
        source = fallback_source
        confidence = 72
        verified = True
    else:
        value = None
        source = "N/A"
        confidence = 0
        verified = False
    return {
        "field": name,
        "value": value,
        "source": source,
        "verified": verified,
        "timestamp": _timestamp(),
        "confidence": confidence,
        "disagreement": disagreement,
    }


def verification_score(fields: dict[str, dict[str, Any]]) -> int:
    """Compute a 0-100 verification score from field envelopes."""
    if not fields:
        return 0
    total = 0
    for payload in fields.values():
        total += int(payload.get("confidence") or 0)
        if payload.get("disagreement"):
            total -= 15
    return max(0, min(100, round(total / len(fields))))


def _espn_events_by_matchup(card_date: str) -> dict[tuple[str, str], dict[str, Any]]:
    scoreboard = espn_client.get_scoreboard(card_date)
    events = scoreboard.get("events") if isinstance(scoreboard.get("events"), list) else []
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        competition = (event.get("competitions") or [{}])[0] if isinstance(event, dict) else {}
        competitors = competition.get("competitors") if isinstance(competition.get("competitors"), list) else []
        teams = []
        for competitor in competitors:
            team = competitor.get("team") or {}
            name = team.get("displayName") or team.get("name") or team.get("shortDisplayName")
            if name:
                teams.append((str(competitor.get("homeAway") or ""), str(name), competitor))
        if len(teams) < 2:
            continue
        away = next((item for item in teams if item[0] == "away"), teams[0])
        home = next((item for item in teams if item[0] == "home"), teams[-1])
        key = (_norm(away[1]), _norm(home[1]))
        mapped[key] = {
            "event": event,
            "event_id": event.get("id"),
            "away_name": away[1],
            "home_name": home[1],
            "away_competitor": away[2],
            "home_competitor": home[2],
        }
    return mapped


def _record_from_espn_competitor(competitor: dict[str, Any]) -> str | None:
    records = competitor.get("records") if isinstance(competitor.get("records"), list) else []
    for record in records:
        summary = record.get("summary")
        if summary:
            return str(summary)
    return None


def _person_name(value: Any) -> str | None:
    """Best-effort name extraction from ESPN person/player fragments."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("displayName", "fullName", "shortName", "name"):
        if value.get(key):
            return str(value[key])
    athlete = value.get("athlete") or value.get("player") or value.get("person")
    return _person_name(athlete)


def _extract_probable_starter(summary: dict[str, Any], side: str) -> str | None:
    """Extract a probable starter from ESPN summary when ESPN exposes one.

    ESPN summary payloads vary over time, so this walks the JSON object and
    looks for common probable/starting pitcher containers attached to a
    homeAway side. This is verification-only and never overrides MLB Stats API.
    """
    candidates: list[str] = []

    def walk(value: Any, current_side: str | None = None) -> None:
        if isinstance(value, dict):
            next_side = str(value.get("homeAway") or current_side or "").lower()
            for key in (
                "probableStartingPitcher",
                "probablePitcher",
                "startingPitcher",
                "starter",
            ):
                name = _person_name(value.get(key))
                if name and (not next_side or next_side == side):
                    candidates.append(name)
            for child in value.values():
                walk(child, next_side)
        elif isinstance(value, list):
            for child in value:
                walk(child, current_side)

    walk(summary)
    return candidates[0] if candidates else None


def _extract_lineup_status(summary: dict[str, Any]) -> str | None:
    """Return a lightweight ESPN summary lineup signal when present."""
    text = json.dumps(summary, ensure_ascii=False).lower() if summary else ""
    if "battingorder" in text or "batting order" in text or "lineup" in text:
        return "available"
    return None


def enrich_mlb_slate_verification(slate: list[dict[str, Any]], card_date: str) -> list[dict[str, Any]]:
    """Attach verification envelopes and scores to every MLB slate game."""
    espn_events = _espn_events_by_matchup(card_date)
    enriched: list[dict[str, Any]] = []
    for game in slate:
        copied = dict(game)
        key = (_norm(copied.get("away_team")), _norm(copied.get("home_team")))
        espn_event = espn_events.get(key, {})
        espn_summary = espn_client.get_summary(espn_event.get("event_id")) if espn_event.get("event_id") else {}
        away_comp = espn_event.get("away_competitor") if isinstance(espn_event.get("away_competitor"), dict) else {}
        home_comp = espn_event.get("home_competitor") if isinstance(espn_event.get("home_competitor"), dict) else {}
        fields = {
            "schedule": verified_field(
                name="schedule",
                primary_value=f"{copied.get('away_team')} @ {copied.get('home_team')}",
                fallback_value=(
                    f"{espn_event.get('away_name')} @ {espn_event.get('home_name')}"
                    if espn_event else None
                ),
            ),
            "game_pk": verified_field(
                name="game_pk",
                primary_value=copied.get("game_pk") or copied.get("game_id"),
                fallback_value=espn_event.get("event_id"),
                compare=False,
            ),
            "away_record": verified_field(
                name="away_record",
                primary_value=copied.get("away_record"),
                fallback_value=_record_from_espn_competitor(away_comp),
            ),
            "home_record": verified_field(
                name="home_record",
                primary_value=copied.get("home_record"),
                fallback_value=_record_from_espn_competitor(home_comp),
            ),
            "away_starter": verified_field(
                name="away_starter",
                primary_value=copied.get("away_pitcher"),
                fallback_value=_extract_probable_starter(espn_summary, "away"),
            ),
            "home_starter": verified_field(
                name="home_starter",
                primary_value=copied.get("home_pitcher"),
                fallback_value=_extract_probable_starter(espn_summary, "home"),
            ),
            "lineups": verified_field(
                name="lineups",
                primary_value=copied.get("lineup_status") or copied.get("official_lineups"),
                fallback_value=_extract_lineup_status(espn_summary),
                compare=False,
            ),
            "weather": verified_field(
                name="weather",
                primary_value=(copied.get("weather") or {}).get("summary") if isinstance(copied.get("weather"), dict) else None,
                fallback_value=None,
                primary_source="Weather API",
            ),
            "odds": verified_field(
                name="odds",
                primary_value="available" if copied.get("odds_status") == "available" or copied.get("best_available_prices") else None,
                fallback_value=None,
                primary_source="Odds API",
            ),
        }
        alerts = [
            f"{payload['field']} disagreement: kept {payload['source']}"
            for payload in fields.values()
            if payload.get("disagreement")
        ]
        copied["verification"] = {
            "score": verification_score(fields),
            "fields": fields,
            "espn_event_id": espn_event.get("event_id"),
            "admin_alerts": alerts,
            "timestamp": _timestamp(),
        }
        # Fill missing records from ESPN only when MLB omitted them.
        if not _present(copied.get("away_record")):
            copied["away_record"] = fields["away_record"].get("value")
        if not _present(copied.get("home_record")):
            copied["home_record"] = fields["home_record"].get("value")
        enriched.append(copied)
    return enriched


def average_verification_score(slate: list[dict[str, Any]]) -> int:
    scores = [
        int((game.get("verification") or {}).get("score") or 0)
        for game in slate
        if isinstance(game, dict)
    ]
    return round(sum(scores) / len(scores)) if scores else 0
