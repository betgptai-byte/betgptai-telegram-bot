"""SP vs Batter Matchup Engine — Baseball Savant Statcast matchup scoring.

Compares each projected/confirmed starting hitter (1-9) against the opposing
starting pitcher using Baseball Savant pitcer and batter metrics.  Produces
per-hitter scores (contact, power, strikeout risk, pitch type, platoon) and
game-level team summaries.

Rules:
- Never fabricate missing data — lower quality grade instead.
- Over and Under must compete via edge scores for team/game totals.
- Public picks require market context; admin intel can show marked inferred leans.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"
MISSING_FIELDS_QUALITY = {
    0: "A",
    1: "A-",
    2: "B+",
    3: "B",
    4: "B-",
    5: "C+",
    6: "C",
}

_GAME_PK_MISMATCH_WARNINGS: list[str] = []


# ── Helpers ────────────────────────────────────────────────────────────────

def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(str(value).replace("%", "").replace(" mph", ""))
        return v
    except (TypeError, ValueError):
        return default


def _safe(value: Any, fallback: str = "Unavailable") -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != UNAVAILABLE else fallback


def _pct(value: Any) -> float:
    """Return a percentage value (0–100) from any numeric or string input."""
    return _num(value)


def _metric_score(value: Any, average: float, weight: float, lower_is_better: bool = False) -> float:
    """Score a metric relative to league-average baseline."""
    n = _num(value)
    if n == 0.0 and value is not None and str(value).strip() not in ("", "0", "0.0", "none"):
        return 0.0
    if n == 0.0:
        return 0.0
    if lower_is_better:
        edge = average - n
    else:
        edge = n - average
    return max(-weight, min(weight, edge * weight))


# ── Data extraction ────────────────────────────────────────────────────────

def _pitcher_metrics(game: dict[str, Any], side: str) -> dict[str, Any]:
    """Extract opposing pitcer metrics for *side* hitters."""
    savant = _dict(game.get("savant"))
    return _dict(savant.get(f"{side}_pitcher"))


def _batter_metrics(game: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Return list of batter dicts from Savant and FanGraphs for *side*."""
    savant = _dict(game.get("savant"))
    fg = _dict(game.get("fangraphs"))
    batter_list: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for batter in _list(savant.get(f"{side}_batters")):
        name = _safe(batter.get("player", "")).lower()
        if name and name not in seen_names:
            seen_names.add(name)
            batter_list.append(dict(batter))
    for hitter in _list(fg.get(f"{side}_hitter_samples")):
        name = _safe(hitter.get("player", "")).lower()
        if name and name not in seen_names:
            seen_names.add(name)
            batter_list.append(dict(hitter))
    return batter_list


def _pitch_type_matchup(game: dict[str, Any], opponent_side: str, batter_side: str) -> list[dict[str, Any]]:
    """Return pitch arsenal for a pitcer facing a team side."""
    savant = _dict(game.get("savant"))
    matchups = _dict(savant.get("pitch_type_matchups"))
    key = f"{opponent_side}_pitcher_vs_{batter_side}"
    matchup = _dict(matchups.get(key))
    return _list(matchup.get("arsenal"))


def _platoon_splits(game: dict[str, Any], side: str) -> dict[str, Any]:
    """Return team handedness splits (xwOBA vs LHP / xwOBA vs RHP) for *side*."""
    savant = _dict(game.get("savant"))
    team = _dict(savant.get(f"{side}_team"))
    return {
        "xwOBA_vs_LHP": _num(team.get("xwOBA vs LHP")),
        "xwOBA_vs_RHP": _num(team.get("xwOBA vs RHP")),
        "has_splits": bool(team.get("xwOBA vs LHP") not in (None, "", UNAVAILABLE) or team.get("xwOBA vs RHP") not in (None, "", UNAVAILABLE)),
    }


def _park_run_environment(game: dict[str, Any]) -> str:
    park = str(game.get("park_factor") or game.get("park_factor_label") or "neutral").lower()
    return park


def _is_hitter_park(park: str) -> bool:
    return any(word in park for word in ("hitter", "hr", "extreme", "friendly"))


def _normalize_edge(value: Any) -> float:
    """Ensure an edge score is 0–100.  If > 100, treat as already-multiplied value and divide by 100."""
    v = _num(value)
    if v > 100:
        v = v / 100.0
    return max(0.0, min(100.0, v))


def _data_quality(missing_count: int, hitters_scanned: int = 0) -> str:
    """Return data quality grade.

    If hitters_scanned is 0, grade is always C or D (never A).
    """
    if hitters_scanned == 0:
        return "D"
    return MISSING_FIELDS_QUALITY.get(missing_count, "C")


def _validate_game_pk(game_pk: Any, pitcher_side: str, game: dict[str, Any]) -> bool:
    """Validate that pitcher metrics belong to the same game_pk.

    Logs a warning if game_pk mismatches on the pitcher side so it is not reused
    from another game.
    """
    pitcher_game_pk = game.get(f"{pitcher_side}_pitcher_game_pk") or game.get("game_pk") or game.get("game_id")
    expected = game.get("game_pk") or game.get("game_id")
    if pitcher_game_pk and expected and str(pitcher_game_pk) != str(expected):
        msg = (
            f"Pitcher game_pk mismatch: {pitcher_side} pitcher belongs to game "
            f"{pitcher_game_pk} but current game is {expected} — skipping matchup"
        )
        _GAME_PK_MISMATCH_WARNINGS.append(msg)
        logger.warning(msg)
        return False
    return True


# ── Scoring functions ──────────────────────────────────────────────────────

def _contact_edge(
    batter: dict[str, Any],
    pitcher: dict[str, Any],
) -> float:
    """Score contact expectation 0–100.

    Batter xBA + low K%  combined with pitcer xBA allowed + contact allowed.
    """
    score = 50.0
    score += _metric_score(batter.get("xBA"), average=0.250, weight=100)
    score += _metric_score(batter.get("xwOBA") or batter.get("wOBA"), average=0.320, weight=60)
    score += _metric_score(batter.get("OPS"), average=0.720, weight=20)
    score += _metric_score(pitcher.get("xBA"), average=0.240, weight=85, lower_is_better=False)
    score += _metric_score(pitcher.get("Hard Hit %"), average=40.0, weight=0.30, lower_is_better=False)
    score += _metric_score(batter.get("Whiff %"), average=25.0, weight=0.25, lower_is_better=True)
    score += _metric_score(batter.get("Chase %"), average=28.0, weight=0.20, lower_is_better=True)
    return max(0.0, min(100.0, score))


def _power_edge(
    batter: dict[str, Any],
    pitcher: dict[str, Any],
) -> float:
    """Score power expectation 0–100.

    Batter xSLG / Barrel% / HardHit% / EV
    vs pitcer xSLG allowed / Barrel% allowed / FB profile
    """
    score = 50.0
    score += _metric_score(batter.get("xSLG"), average=0.410, weight=60)
    score += _metric_score(batter.get("Barrel %") or batter.get("Barrel%"), average=8.0, weight=2.0)
    score += _metric_score(batter.get("Hard Hit %") or batter.get("Hard%"), average=40.0, weight=0.55)
    score += _metric_score(batter.get("Exit Velocity"), average=88.0, weight=1.25)
    score += _metric_score(batter.get("ISO"), average=0.160, weight=70)
    score += _metric_score(pitcher.get("xSLG"), average=0.410, weight=45, lower_is_better=False)
    score += _metric_score(pitcher.get("Barrel %"), average=8.0, weight=1.4, lower_is_better=False)
    score += _metric_score(pitcher.get("Hard Hit %"), average=40.0, weight=0.30, lower_is_better=False)
    return max(0.0, min(100.0, score))


def _strikeout_risk(
    batter: dict[str, Any],
    pitcher: dict[str, Any],
) -> float:
    """Score strikeout risk 0–100 (higher = batter more likely to K).

    Batter K% / Whiff% / chase%
    vs pitcer Whiff% / chase% / K%
    """
    score = 50.0
    score += _metric_score(batter.get("Whiff %"), average=25.0, weight=0.35)
    score += _metric_score(batter.get("Chase %"), average=28.0, weight=0.25)
    score += _metric_score(batter.get("K%"), average=22.0, weight=0.35)
    score += _metric_score(pitcher.get("Whiff %"), average=25.0, weight=0.50, lower_is_better=False)
    score += _metric_score(pitcher.get("Chase %"), average=28.0, weight=0.30, lower_is_better=False)
    score += _metric_score(pitcher.get("K%") or batter.get("K%"), average=22.0, weight=0.40, lower_is_better=False)
    return max(0.0, min(100.0, score))


def _pitch_type_edge(
    pitcher_arsenal: list[dict[str, Any]],
) -> float:
    """Score pitcer arsenal quality 0–100 vs a team side.

    Uses top-3 pitches by usage — higher whiff rate and lower xwOBA = stronger edge.
    """
    sorted_pitches = sorted(
        pitcher_arsenal,
        key=lambda p: _num(p.get("usage_count") or p.get("usage") or 0),
        reverse=True,
    )
    top3 = sorted_pitches[:3]
    if not top3:
        return 50.0
    score = 50.0
    for pitch in top3:
        whiff = _pct(pitch.get("whiff_rate"))
        xwoba = _num(pitch.get("xwOBA_allowed"))
        velo = _num(pitch.get("velocity"))
        if whiff > 0:
            score += (whiff - 25) * 0.20
        if xwoba > 0:
            score += (0.350 - xwoba) * 50
        if velo >= 95:
            score += 2
        elif velo >= 92:
            score += 1
    return max(0.0, min(100.0, score / max(1, len(top3))))


def _platoon_edge(
    batter: dict[str, Any],
    pitcher: dict[str, Any],
    team_splits: dict[str, Any],
) -> float:
    """Score platoon advantage 0–100.

    Uses team-level xwOBA splits vs LHP/RHP since individual batter
    handedness splits are not available in the current enrichment.
    Returns neutral (50) if no split data exists.
    """
    if not team_splits.get("has_splits"):
        return 50.0
    xwoba_lhp = team_splits.get("xwOBA_vs_LHP", 0)
    xwoba_rhp = team_splits.get("xwOBA_vs_RHP", 0)
    avg_xwoba = (xwoba_lhp + xwoba_rhp) / 2 if (xwoba_lhp and xwoba_rhp) else max(xwoba_lhp, xwoba_rhp)
    if avg_xwoba == 0:
        return 50.0
    score = 50.0 + (avg_xwoba - 0.315) * 200
    return max(0.0, min(100.0, score))


def _total_bases_score(
    contact_edge: float,
    power_edge: float,
    lineup_spot: int,
) -> float:
    """Score total-bases expectation 0–100.

    Blend of contact and power, boosted for top of lineup.
    """
    base = contact_edge * 0.35 + power_edge * 0.65
    if lineup_spot <= 2:
        base += 8
    elif lineup_spot <= 5:
        base += 4
    else:
        base -= 3
    return max(0.0, min(100.0, base))


def _missing_field_count(batter: dict[str, Any], pitcher: dict[str, Any]) -> int:
    """Count unavailable metrics that reduce data quality."""
    fields = [
        batter.get("xBA"), batter.get("xSLG"), batter.get("xwOBA"),
        batter.get("Barrel %"), batter.get("Hard Hit %"),
        batter.get("Whiff %"), batter.get("Chase %"), batter.get("Exit Velocity"),
        pitcher.get("xBA"), pitcher.get("xSLG"), pitcher.get("Barrel %"),
        pitcher.get("Hard Hit %"), pitcher.get("Whiff %"), pitcher.get("Chase %"),
    ]
    return sum(1 for f in fields if f in (None, "", UNAVAILABLE))


def _missing_fields_list(batter: dict[str, Any], pitcher: dict[str, Any]) -> list[str]:
    """List of missing field names for debug output."""
    missing = []
    bfields = {
        "xBA": batter.get("xBA"), "xSLG": batter.get("xSLG"),
        "xwOBA": batter.get("xwOBA"), "Barrel %": batter.get("Barrel %"),
        "Hard Hit %": batter.get("Hard Hit %"),
        "Whiff %": batter.get("Whiff %"), "Chase %": batter.get("Chase %"),
        "Exit Velocity": batter.get("Exit Velocity"),
    }
    pfields = {
        "P-xBA": pitcher.get("xBA"), "P-xSLG": pitcher.get("xSLG"),
        "P-Barrel %": pitcher.get("Barrel %"), "P-Hard Hit %": pitcher.get("Hard Hit %"),
        "P-Whiff %": pitcher.get("Whiff %"), "P-Chase %": pitcher.get("Chase %"),
    }
    for name, val in {**bfields, **pfields}.items():
        if val in (None, "", UNAVAILABLE):
            missing.append(name)
    return missing


# ── Per-hitter matchup ─────────────────────────────────────────────────────

def _build_hitter_matchup(
    batter: dict[str, Any],
    pitcher: dict[str, Any],
    pitcher_arsenal: list[dict[str, Any]],
    team_splits: dict[str, Any],
    lineup_spot: int,
    lineup_status: str,
    side: str,
    opponent: str,
    pitcher_name: str,
    park: str,
) -> dict[str, Any]:
    """Build a single per-hitter-vs-SP matchup dict."""
    contact = _normalize_edge(_contact_edge(batter, pitcher))
    power = _normalize_edge(_power_edge(batter, pitcher))
    k_risk = _normalize_edge(_strikeout_risk(batter, pitcher))
    pitch_type = _normalize_edge(_pitch_type_edge(pitcher_arsenal))
    platoon = _normalize_edge(_platoon_edge(batter, pitcher, team_splits))

    total_bases = _total_bases_score(contact, power, lineup_spot)

    # Best market determination
    hit_prop_score = contact
    hr_score = power * 0.75 + contact * 0.25
    k_risk_inverted = 100 - k_risk  # higher = better for the batter

    max_score = max(hit_prop_score, hr_score, total_bases)
    if max_score == hit_prop_score:
        best_market = "hit_prop"
        market_reason = "Contact profile and pitcer contact allowed suggest hit upside."
    elif max_score == hr_score:
        best_market = "hr_watch"
        market_reason = "Power profile and pitcer barrel/xSLG allowed suggest HR risk."
    elif max_score == total_bases:
        best_market = "total_bases"
        market_reason = "Balanced contact and power suggest total-bases value."
    else:
        best_market = "pass"
        market_reason = "No market edge — scores below threshold."

    risk = "Low" if k_risk < 40 else "Medium" if k_risk < 65 else "High"
    missing = _missing_fields_list(batter, pitcher)
    dq = _data_quality(len(missing), hitters_scanned=1)

    reasons = []
    if contact >= 70:
        reasons.append(f"Contact edge {contact:.0f}")
    if power >= 70:
        reasons.append(f"Power edge {power:.0f}")
    if k_risk >= 65:
        reasons.append(f"K risk {k_risk:.0f}")
    if pitch_type >= 65:
        reasons.append(f"Pitch-type mismatch {pitch_type:.0f}")
    if _is_hitter_park(park):
        reasons.append("Hitter park environment")

    return {
        "player_name": _safe(batter.get("player") or batter.get("Name") or batter.get("name"), "Batter"),
        "player_id": batter.get("player_id") or batter.get("id"),
        "team": _safe(side.capitalize()),
        "opponent": opponent,
        "opposing_pitcher": pitcher_name,
        "lineup_spot": lineup_spot,
        "lineup_status": lineup_status,
        "batter_hand": None,
        "pitcher_hand": None,
        "contact_edge_score": round(contact),
        "power_edge_score": round(power),
        "strikeout_risk_score": round(k_risk),
        "pitch_type_edge_score": round(pitch_type),
        "platoon_edge_score": round(platoon),
        "overall_hit_score": round(hit_prop_score),
        "overall_hr_score": round(hr_score),
        "total_bases_score": round(total_bases),
        "risk_level": risk,
        "best_market": best_market,
        "reasons": reasons,
        "missing_fields": missing,
        "data_quality_grade": dq,
    }


# ── Game-level summary ─────────────────────────────────────────────────────

def _game_team_side_matchup(
    game: dict[str, Any],
    batter_side: str,
    pitcher_side: str,
    lineup_label: str,
    game_pk: Any = None,
) -> dict[str, Any]:
    """Build matchup output for one side (away hitters vs home SP, etc.)."""
    if game_pk is not None and not _validate_game_pk(game_pk, pitcher_side, game):
        return {
            "side": batter_side,
            "team": _safe(game.get(f"{batter_side}_team"), "Team"),
            "opposing_pitcher": "MISMATCH",
            "hitters_scanned": 0,
            "hitters_qualified": 0,
            "top_hit_edges": [],
            "top_hr_edges": [],
            "top_total_bases_edges": [],
            "rejected_hitters": [],
            "team_contact_advantage": 0,
            "team_power_advantage": 0,
            "team_k_risk": 0,
            "recommended_team_total_side": "pass",
            "data_quality_grade": "D",
        }
    pitcher = _pitcher_metrics(game, pitcher_side)
    batters = _batter_metrics(game, batter_side)
    arsenal = _pitch_type_matchup(game, pitcher_side, batter_side)
    splits = _platoon_splits(game, batter_side)
    park = _park_run_environment(game)
    pitcher_name = _safe(game.get(f"{pitcher_side}_pitcher"), "TBD")
    opponent_team = _safe(game.get(f"{batter_side}_team"), "Team")

    hit_edges: list[dict[str, Any]] = []
    hr_edges: list[dict[str, Any]] = []
    tb_edges: list[dict[str, Any]] = []
    rejected: list[str] = []
    total_contact = 0.0
    total_power = 0.0
    total_k_risk = 0.0
    lineup_status = "confirmed" if game.get("lineups") not in (None, "", UNAVAILABLE, [], {}) else "projected"

    for idx, batter in enumerate(batters[:9], start=1):
        mu = _build_hitter_matchup(
            batter, pitcher, arsenal, splits, idx, lineup_status,
            batter_side, opponent_team, pitcher_name, park,
        )
        if mu.get("best_market") == "pass":
            rejected.append(f"{mu['player_name']}: all scores below threshold")
            continue
        hit_edges.append(mu)
        hr_edges.append(mu)
        tb_edges.append(mu)
        total_contact += mu["contact_edge_score"]
        total_power += mu["power_edge_score"]
        total_k_risk += mu["strikeout_risk_score"]

    n = max(1, len(hit_edges))
    avg_contact = total_contact / n
    avg_power = total_power / n
    avg_k_risk = total_k_risk / n

    # Team total lean
    team_total_side = "pass"
    if avg_power >= 65 and avg_contact >= 55:
        team_total_side = "over"
    elif avg_power < 40 and avg_contact < 45:
        team_total_side = "under"

    # Game total lean (combine both sides later — placeholder)
    game_total_side = "pass"

    hit_edges.sort(key=lambda r: r.get("overall_hit_score", 0), reverse=True)
    hr_edges.sort(key=lambda r: r.get("overall_hr_score", 0), reverse=True)
    tb_edges.sort(key=lambda r: r.get("total_bases_score", 0), reverse=True)

    all_missing = sum(len(mu.get("missing_fields", [])) for mu in hit_edges)
    total_possible = n * 14  # 14 checked fields per hitter
    dq = _data_quality(all_missing // max(1, n), hitters_scanned=len(batters)) if n else "D"

    return {
        "side": batter_side,
        "team": opponent_team,
        "opposing_pitcher": pitcher_name,
        "hitters_scanned": len(batters),
        "hitters_qualified": len([mu for mu in hit_edges if mu["overall_hit_score"] >= 50]),
        "top_hit_edges": hit_edges[:5],
        "top_hr_edges": hr_edges[:5],
        "top_total_bases_edges": tb_edges[:5],
        "rejected_hitters": rejected,
        "team_contact_advantage": round(avg_contact),
        "team_power_advantage": round(avg_power),
        "team_k_risk": round(avg_k_risk),
        "recommended_team_total_side": team_total_side,
        "data_quality_grade": dq,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def build_sp_batter_matchups(game: dict[str, Any]) -> dict[str, Any]:
    """Build SP vs Batter matchup output for one game.

    Returns a dict with ``away_vs_home_sp``, ``home_vs_away_sp``, and
    ``game_level`` summaries.  Validates that pitcher metrics belong to the
    same game_pk and never reuses pitcher_context from another game.
    """
    game_pk = game.get("game_pk") or game.get("game_id")
    away = _game_team_side_matchup(game, "away", "home", "Away Hitters vs Home SP", game_pk=game_pk)
    home = _game_team_side_matchup(game, "home", "away", "Home Hitters vs Away SP", game_pk=game_pk)

    # Game total: combine both sides
    game_total_side = "pass"
    combined_contact = (away.get("team_contact_advantage", 50) + home.get("team_contact_advantage", 50)) / 2
    combined_power = (away.get("team_power_advantage", 50) + home.get("team_power_advantage", 50)) / 2
    if combined_power >= 65 and combined_contact >= 55:
        game_total_side = "over"
    elif combined_power < 40 and combined_contact < 45:
        game_total_side = "under"

    game_level = {
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "total_hitters_scanned": away["hitters_scanned"] + home["hitters_scanned"],
        "total_hitters_qualified": away["hitters_qualified"] + home["hitters_qualified"],
        "combined_contact_advantage": round(combined_contact),
        "combined_power_advantage": round(combined_power),
        "recommended_game_total_side": game_total_side,
        "data_quality_grade": away["data_quality_grade"],
    }

    return {
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "matchup": game_level["matchup"],
        "away_vs_home_sp": away,
        "home_vs_away_sp": home,
        "game_level": game_level,
    }


def build_slate_matchups(slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Build matchups for an entire slate.

    Returns:
        games — list of per-game matchup dicts
        debug — aggregated debug counters
    """
    games = []
    _GAME_PK_MISMATCH_WARNINGS.clear()
    debug = {
        "games_scanned": 0,
        "hitters_scanned": 0,
        "hitters_qualified": 0,
        "pitcher_metrics_found": 0,
        "batter_metrics_found": 0,
        "pitch_type_matchups_found": 0,
        "missing_fields_total": 0,
        "rejected_hitters": [],
        "game_pk_mismatches": _GAME_PK_MISMATCH_WARNINGS,
    }
    for game in slate:
        result = build_sp_batter_matchups(game)
        games.append(result)
        debug["games_scanned"] += 1
        for side_key in ("away_vs_home_sp", "home_vs_away_sp"):
            side = result.get(side_key) or {}
            debug["hitters_scanned"] += side.get("hitters_scanned", 0)
            debug["hitters_qualified"] += side.get("hitters_qualified", 0)
            debug["rejected_hitters"].extend(side.get("rejected_hitters", []))
            if side.get("opposing_pitcher") != "TBD":
                debug["pitcher_metrics_found"] += 1
            for mu in side.get("top_hit_edges", []):
                debug["batter_metrics_found"] += 1
                debug["missing_fields_total"] += len(mu.get("missing_fields", []))
        pt = _dict(game.get("savant", {}).get("pitch_type_matchups"))
        if pt:
            debug["pitch_type_matchups_found"] += 1

    return {"games": games, "debug": debug}
