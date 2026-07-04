"""Pregame/results Telegram channel scheduler for BETGPTAI.

BETGPTAI is a pregame analysis platform, not a live score bot. This scheduler
does not post live scores, inning updates, score notifications, in-game
Telegram edits, or game-progress messages.

It performs two automated jobs:
1. Generate owner-approval previews 45 minutes before the first scheduled game.
2. After all saved official games are final, grade picks and post results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ai_analysis import analyze_mlb_slate, get_last_analysis_metadata
from ai_learning_engine import render_learning_report, run_learning_review
from card_time import EASTERN, official_sports_date
from daily_workflow import (
    generate_cards_job,
    post_cards_job,
    pregame_verify_job,
    schedule_times,
    time_debug_payload,
    workflow_status,
)
from game_time import parse_game_time
from mlb_auto_image import prepare_mlb_auto_image
from mlb_data import get_combined_slate, get_mlb_schedule
from model_report import save_model_report
from results_tracker import (
    build_daily_results_dashboard,
    display_date,
    grade_mlb_picks_for_date,
    load_picks,
    save_official_picks,
)
from soccer_analysis import analyze_soccer_slate
from soccer_data import get_soccer_schedule, get_soccer_slate
from storage import data_file
from thesportsdb_data import thesportsdb_api_key
from time_utils import get_app_timezone, now_et


POSTING_LOG_FILE = data_file("posting_log.json")
DIVIDER = "━━━━━━━━━━━━"
_CARD_CACHE: dict[str, dict[str, Any]] = {}
AUTO_RESULTS_ENABLED_KEY = "auto_results_enabled"
RESULTS_POST_DESTINATIONS = ("FREE_CHANNEL_ID", "VIP_CHANNEL_ID")
OFFICIAL_RESULTS_SOURCES = {
    "today",
    "mlb_auto",
    "generate_today",
    "tap_menu_mlb_card",
    "scheduled_generate",
}
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
REQUEST_TIMEOUT = 20


def _read_log() -> dict[str, Any]:
    """Read the persistent posting log, recovering safely from bad JSON."""
    if not POSTING_LOG_FILE.exists():
        return {}
    try:
        payload = json.loads(POSTING_LOG_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        logging.exception("Could not read posting_log.json")
        return {}


def _write_log(payload: dict[str, Any]) -> None:
    """Atomically save the posting log so an interrupted write cannot corrupt it."""
    temporary = POSTING_LOG_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(POSTING_LOG_FILE)


def _job_done(log: dict[str, Any], day: str, job_id: str) -> bool:
    entry = log.get(day, {}).get(job_id, {})
    return isinstance(entry, dict) and entry.get("status") in {"sent", "skipped"}


def _record_job(
    log: dict[str, Any], day: str, job_id: str, status: str, detail: str = ""
) -> None:
    log.setdefault(day, {})[job_id] = {
        "status": status,
        "recorded_at": datetime.now(EASTERN).isoformat(),
        "detail": detail,
    }
    _write_log(log)


def _destination(environment_name: str) -> int | str:
    """Load a Telegram channel ID or @username from the environment."""
    value = os.getenv(environment_name, "").strip()
    if not value:
        raise ValueError(f"{environment_name} is missing from .env.")
    if value.startswith("@"):
        return value
    numeric = int(value)
    return -numeric if numeric > 0 and value.startswith("100") else numeric


def auto_results_enabled() -> bool:
    """Return whether end-of-day automatic results posting is enabled."""
    return bool(_read_log().get(AUTO_RESULTS_ENABLED_KEY, True))


def set_auto_results_enabled(enabled: bool) -> None:
    """Owner control for the automatic daily results poster."""
    log = _read_log()
    log[AUTO_RESULTS_ENABLED_KEY] = enabled
    log["auto_results_updated_at"] = datetime.now(EASTERN).isoformat()
    _write_log(log)


def _results_posted_key(day: str) -> str:
    """Top-level posting flag requested for duplicate prevention."""
    return f"results_posted_{day}"


def _results_posted(log: dict[str, Any], day: str) -> bool:
    """Return True if today's automatic results card already posted."""
    return bool(log.get(_results_posted_key(day)))


def _mark_results_posted(log: dict[str, Any], day: str) -> None:
    """Persist the one-shot results posted flag."""
    log[_results_posted_key(day)] = True
    log.setdefault(day, {})["auto_results"] = {
        "status": "sent",
        "recorded_at": datetime.now(EASTERN).isoformat(),
    }
    _write_log(log)


async def _send_long(bot: Any, chat_id: int | str, text: str) -> None:
    """Send a long card in Telegram-safe chunks."""
    remaining = text.strip()
    while remaining:
        if len(remaining) <= 3900:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n\n", 0, 3900)
            if split_at < 1:
                split_at = remaining.rfind("\n", 0, 3900)
            if split_at < 1:
                split_at = 3900
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip()
        await bot.send_message(chat_id=chat_id, text=chunk)


async def _mlb_card(day: str) -> tuple[str, list[dict[str, Any]]]:
    """Generate one consistent daily MLB card and reuse it for every channel."""
    cached = _CARD_CACHE.get(day, {})
    if isinstance(cached.get("mlb_card"), str):
        return cached["mlb_card"], cached["mlb_slate"]
    slate = await asyncio.to_thread(
        get_combined_slate,
        os.getenv("ODDS_API_KEY", ""),
        game_date=day,
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    if not slate:
        raise ValueError("No MLB games are available for this sports day.")
    card = await analyze_mlb_slate(
        slate,
        os.getenv("OPENAI_API_KEY", ""),
        os.getenv("ANTHROPIC_API_KEY", ""),
    )
    await asyncio.to_thread(
        save_model_report, day, slate, card, get_last_analysis_metadata()
    )
    try:
        saved = await asyncio.to_thread(save_official_picks, card, slate, day)
        print(f"Saved {saved} official picks to picks.json", flush=True)
    except Exception:
        # Posting remains available even when the tracker needs owner attention.
        logging.exception("Could not save scheduled official picks")
    _CARD_CACHE.setdefault(day, {}).update({"mlb_card": card, "mlb_slate": slate})
    return card, slate


async def _soccer_card(day: str) -> str:
    """Generate and cache the public soccer card for its scheduled posting."""
    cached = _CARD_CACHE.get(day, {})
    if isinstance(cached.get("soccer_card"), str):
        return cached["soccer_card"]
    slate = await asyncio.to_thread(
        get_soccer_slate,
        os.getenv("FOOTBALL_DATA_API_KEY", ""),
        os.getenv("ODDS_API_KEY", ""),
        game_date=day,
        sports_db_api_key=thesportsdb_api_key(),
        serpapi_key=os.getenv("SERPAPI_KEY", ""),
        api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
    )
    if not slate:
        raise ValueError("No soccer matches are available for this sports day.")
    card = await analyze_soccer_slate(
        slate,
        os.getenv("OPENAI_API_KEY", ""),
        "public",
        os.getenv("ANTHROPIC_API_KEY", ""),
    )
    _CARD_CACHE.setdefault(day, {})["soccer_card"] = card
    return card


async def _content_for(job_type: str, day: str) -> str:
    """Build only the card required by one due posting job."""
    if job_type == "soccer":
        return await _soccer_card(day)
    if job_type == "results":
        await asyncio.to_thread(grade_mlb_picks_for_date, day)
        return await asyncio.to_thread(build_daily_results_dashboard, day)

    card, _slate = await _mlb_card(day)
    title = "🔥 FULL MLB CARD" if job_type == "full_mlb" else "⚾ FREE MLB CARD"
    return f"{title}\n\n{card}"


def _is_final(game: dict[str, Any]) -> bool:
    status = str(game.get("status", "")).lower()
    return any(value in status for value in ("final", "game over", "completed early"))


def _timed_jobs(
    first_mlb: datetime | None, first_soccer: datetime | None
) -> list[dict[str, Any]]:
    """Create the pregame-only channel plan relative to actual first starts."""
    jobs: list[dict[str, Any]] = []

    def add(
        destination: str,
        name: str,
        job_type: str,
        due: datetime,
        first_start: datetime,
    ) -> None:
        jobs.append(
            {
                "id": f"{destination}_{name}",
                "destination": destination,
                "type": job_type,
                "due": due,
                "first_start": first_start,
            }
        )

    if first_mlb:
        due = first_mlb - timedelta(minutes=45)
        add("MY_TELEGRAM_ID", "mlb_pregame_owner_preview", "full_mlb", due, first_mlb)
    if first_soccer:
        due = first_soccer - timedelta(minutes=45)
        add("MY_TELEGRAM_ID", "soccer_pregame_owner_preview", "soccer", due, first_soccer)
    return jobs


async def _post_job(bot: Any, log: dict[str, Any], day: str, job: dict[str, Any]) -> None:
    """Send one due job and record success only after Telegram accepts it."""
    try:
        chat_id = _destination(job["destination"])
        content = await _content_for(job["type"], day)
        await _send_long(
            bot,
            chat_id,
            "🧪 BETGPTAI OWNER PREGAME PREVIEW\n\n"
            "Review and approve before public posting.\n\n"
            f"{content}",
        )
        if job["type"] in {"free_mlb", "full_mlb"}:
            card, _slate = await _mlb_card(day)
            image_result = await asyncio.to_thread(prepare_mlb_auto_image, card, day)
            image_path = image_result.get("image_path")
            if image_path and Path(str(image_path)).exists():
                with Path(str(image_path)).open("rb") as image_file:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=image_file,
                        caption="🖼 MLB Anime Card Preview — owner approval only.",
                    )
            elif image_result.get("prompt"):
                await _send_long(
                    bot,
                    chat_id,
                    "🖼 MLB Anime Card Prompt — owner approval only.\n\n"
                    f"{image_result.get('prompt')}",
                )
        _record_job(log, day, job["id"], "sent")
        logging.info("Sent scheduled post %s for %s", job["id"], day)
    except Exception as error:
        logging.exception("Scheduled post %s failed: %s", job["id"], error)


def _flatten_game_pks(value: Any) -> set[int]:
    """Return every integer game_pk from scalar or parlay-list values."""
    if isinstance(value, list):
        game_ids: set[int] = set()
        for item in value:
            game_ids.update(_flatten_game_pks(item))
        return game_ids
    try:
        return {int(str(value))}
    except (TypeError, ValueError):
        return set()


def _today_official_mlb_picks(day: str) -> list[dict[str, Any]]:
    """Load today's saved official MLB picks from allowed public card sources."""
    return [
        pick for pick in load_picks()
        if pick.get("sport", "mlb") == "mlb"
        and pick.get("category") != "parlay_leg"
        and str(pick.get("card_date") or pick.get("date") or "") == day
        and str(pick.get("source_command") or "") in OFFICIAL_RESULTS_SOURCES
    ]


def _game_pks_for_picks(picks: list[dict[str, Any]]) -> set[int]:
    """Collect every related game_pk from official picks."""
    game_ids: set[int] = set()
    for pick in picks:
        game_ids.update(_flatten_game_pks(pick.get("game_pk") or pick.get("game_id")))
        for leg in pick.get("legs", []) if isinstance(pick.get("legs"), list) else []:
            if isinstance(leg, dict):
                game_ids.update(_flatten_game_pks(leg.get("game_pk") or leg.get("game_id")))
    return game_ids


def _fetch_mlb_statuses(day: str) -> dict[int, str]:
    """Fetch MLB game statuses from MLB Stats API."""
    response = requests.get(
        MLB_SCHEDULE_URL,
        params={"sportId": "1", "date": day},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    statuses: dict[int, str] = {}
    for group in payload.get("dates", []):
        for game in group.get("games", []):
            game_id = game.get("gamePk")
            if isinstance(game_id, int):
                statuses[game_id] = str(
                    game.get("status", {}).get("abstractGameState")
                    or game.get("status", {}).get("detailedState")
                    or ""
                )
    return statuses


def _walk_strings(value: Any) -> list[str]:
    """Collect strings from a nested JSON value for flexible SerpApi parsing."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_walk_strings(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_walk_strings(item))
        return strings
    return []


def _fetch_mlb_statuses_from_serpapi(
    day: str, picks: list[dict[str, Any]]
) -> dict[int, str]:
    """Best-effort SerpApi fallback for final-status checks only."""
    api_key = os.getenv("SERPAPI_KEY", "").strip()
    if not api_key:
        return {}
    response = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google",
            "q": f"MLB scores {display_date(day)}",
            "api_key": api_key,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    games = payload.get("sports_results", {}).get("games", [])
    if not isinstance(games, list):
        return {}

    statuses: dict[int, str] = {}
    for pick in picks:
        game_ids = _flatten_game_pks(pick.get("game_pk") or pick.get("game_id"))
        if not game_ids:
            continue
        away = str(pick.get("away_team") or "").lower()
        home = str(pick.get("home_team") or "").lower()
        if not away or not home:
            continue
        for game in games:
            game_text = " ".join(_walk_strings(game)).lower()
            if away in game_text and home in game_text:
                status = "Final" if "final" in game_text else "In Progress"
                for game_id in game_ids:
                    statuses[game_id] = status
    return statuses


def _fetch_mlb_statuses_with_fallback(
    day: str, picks: list[dict[str, Any]]
) -> tuple[dict[int, str], str]:
    """Primary status check is MLB Stats; SerpApi is a quiet optional fallback."""
    try:
        return _fetch_mlb_statuses(day), "mlb_stats"
    except Exception:
        logging.exception("MLB Stats API status check failed for auto results")
        if os.getenv("SERPAPI_KEY", "").strip():
            try:
                fallback = _fetch_mlb_statuses_from_serpapi(day, picks)
                if fallback:
                    return fallback, "serpapi"
            except Exception:
                logging.exception("SerpApi fallback status check failed")
        return {}, "unavailable"


def official_results_ready(day: str | None = None) -> dict[str, Any]:
    """Inspect whether saved official MLB picks can be graded and posted."""
    selected_day = day or official_sports_date().isoformat()
    picks = _today_official_mlb_picks(selected_day)
    if not picks:
        logging.info("No official picks found for today.")
        return {
            "ready": False,
            "reason": "No official picks found for today.",
            "day": selected_day,
            "picks": 0,
            "games": 0,
            "pending_games": [],
        }

    game_ids = _game_pks_for_picks(picks)
    statuses, source = _fetch_mlb_statuses_with_fallback(selected_day, picks)
    if not statuses:
        return {
            "ready": False,
            "reason": "Game statuses unavailable.",
            "day": selected_day,
            "picks": len(picks),
            "games": len(game_ids),
            "pending_games": sorted(game_ids),
            "source": source,
        }

    pending_games = [
        game_id for game_id in sorted(game_ids)
        if str(statuses.get(game_id, "")).lower() != "final"
    ]
    return {
        "ready": not pending_games,
        "reason": "All saved games final." if not pending_games else "Games still pending.",
        "day": selected_day,
        "picks": len(picks),
        "games": len(game_ids),
        "pending_games": pending_games,
        "source": source,
    }


def _summary_line(summary: dict[str, Any]) -> str:
    """Compact W-L-P line for the automatic channel results card."""
    return (
        f"W-L-P: {summary.get('wins', 0)}-{summary.get('losses', 0)}-"
        f"{summary.get('pushes', 0)}\n"
        f"Win %: {summary.get('win_percentage', 0):g}%\n"
        f"Profit Units: {summary.get('profit_units', 0):+g}"
    )


def _market_line(summary: dict[str, Any]) -> str:
    """One-line market record."""
    return (
        f"{summary.get('wins', 0)}-{summary.get('losses', 0)}-"
        f"{summary.get('pushes', 0)} ({summary.get('profit_units', 0):+g}u)"
    )


def _build_auto_results_card(day: str) -> str:
    """Build the exact clean daily results post from graded saved picks."""
    from results_tracker import _summaries_for_picks  # Local to avoid public export.

    all_today = _today_official_mlb_picks(day)
    summaries = _summaries_for_picks(all_today)
    overall = summaries["overall"]
    return (
        "📊 BETGPTAI DAILY RESULTS\n"
        f"📅 Date: {display_date(day)}\n\n"
        "Today’s Card:\n"
        f"{_summary_line(overall)}\n\n"
        f"Moneyline: {_market_line(summaries['moneyline'])}\n"
        f"F5 Moneyline: {_market_line(summaries['f5_moneyline'])}\n"
        f"Runline: {_market_line(summaries['runline'])}\n"
        f"Totals: {_market_line(summaries['totals'])}\n"
        f"Team Totals: {_market_line(summaries['team_totals'])}\n"
        "Player Props: 0-0-0 (+0u)\n\n"
        f"Pending:\n{overall.get('pending', 0)}\n\n"
        f"{DIVIDER}\n\n"
        "Singles-first approach.\n"
        "Parlays carry higher risk."
    )


async def post_daily_results_if_ready(
    bot: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Grade and post today's results after all saved official games are final."""
    current = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    day = official_sports_date(current).isoformat()
    log = _read_log()

    if not force and not auto_results_enabled():
        return {"posted": False, "reason": "Automatic results disabled.", "day": day}
    if _results_posted(log, day):
        return {"posted": False, "reason": "Results already posted.", "day": day}

    check_start = current.replace(hour=22, minute=30, second=0, microsecond=0)
    sports_day = datetime.fromisoformat(day).replace(tzinfo=EASTERN)
    check_start = check_start.replace(
        year=sports_day.year, month=sports_day.month, day=sports_day.day
    )
    if not force and current < check_start:
        return {"posted": False, "reason": "Waiting until 10:30 PM ET.", "day": day}

    last_checked_key = f"results_last_checked_{day}"
    last_checked = parse_game_time(log.get(last_checked_key))
    if not force and last_checked and current < last_checked + timedelta(minutes=15):
        return {"posted": False, "reason": "Waiting for next 15-minute check.", "day": day}
    log[last_checked_key] = current.isoformat()
    _write_log(log)

    readiness = await asyncio.to_thread(official_results_ready, day)
    if not readiness.get("ready"):
        return {"posted": False, **readiness}

    await asyncio.to_thread(grade_mlb_picks_for_date, day)
    try:
        learning_report = await asyncio.to_thread(run_learning_review, day)
        admin_id = os.getenv("MY_TELEGRAM_ID", "").strip()
        if admin_id:
            await _send_long(
                bot,
                int(admin_id),
                render_learning_report(learning_report),
            )
    except Exception:
        logging.exception("AI Learning review failed after automatic grading")
    card = await asyncio.to_thread(_build_auto_results_card, day)
    sent_to: list[str] = []
    for environment_name in RESULTS_POST_DESTINATIONS:
        try:
            await _send_long(bot, _destination(environment_name), card)
            sent_to.append(environment_name)
        except Exception:
            logging.exception("Could not post automatic results to %s", environment_name)
    if not sent_to:
        return {"posted": False, "reason": "No destinations accepted the results post.", "day": day}

    log = _read_log()
    _mark_results_posted(log, day)
    return {
        "posted": True,
        "reason": "Results posted.",
        "day": day,
        "sent_to": sent_to,
        "picks": readiness.get("picks", 0),
        "games": readiness.get("games", 0),
    }


def results_auto_status_text(day: str | None = None) -> str:
    """Owner-facing status summary for the automatic results poster."""
    selected_day = day or official_sports_date().isoformat()
    log = _read_log()
    readiness = official_results_ready(selected_day)
    return (
        "📊 BETGPTAI AUTO RESULTS STATUS\n\n"
        f"Enabled: {'✅ Yes' if auto_results_enabled() else '❌ No'}\n"
        f"Date: {display_date(selected_day)}\n"
        f"Posted: {'✅ Yes' if _results_posted(log, selected_day) else '❌ No'}\n"
        f"Official Picks: {readiness.get('picks', 0)}\n"
        f"Tracked Games: {readiness.get('games', 0)}\n"
        f"Ready: {'✅ Yes' if readiness.get('ready') else '❌ No'}\n"
        f"Pending Games: {len(readiness.get('pending_games', []))}\n"
        f"Reason: {readiness.get('reason', 'Unavailable')}"
    )


def time_debug_text(day: str | None = None) -> str:
    """Owner-facing timezone/scheduler diagnostics."""
    payload = time_debug_payload(day)
    return (
        "🕒 BETGPTAI TIME DEBUG\n\n"
        f"UTC now: {payload.get('utc_now')}\n"
        f"ET now: {payload.get('et_now')}\n"
        f"Server TZ env: {payload.get('server_tz_env')}\n"
        f"APP_TIMEZONE: {payload.get('app_timezone')}\n\n"
        f"First MLB game ET: {payload.get('first_pitch_et')}\n"
        f"T-50 verify time: {payload.get('verify_time_et')}\n"
        f"T-45 generate time: {payload.get('generate_time_et')}\n"
        f"T-43 post time: {payload.get('post_time_et')}\n\n"
        f"Scheduler running: {'✅ Yes' if payload.get('scheduler_running') else '❌ No'}\n"
        f"Jobs registered: {', '.join(payload.get('jobs_registered') or [])}\n"
        f"Seconds until next job: {payload.get('seconds_until_next_job')}"
    )


def scheduler_status_text(day: str | None = None) -> str:
    """Owner-facing status for the 3-step pregame workflow."""
    payload = workflow_status(day)
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    posting = payload.get("posting") if isinstance(payload.get("posting"), dict) else {}
    times = payload.get("times") if isinstance(payload.get("times"), dict) else {}
    return (
        "📡 BETGPTAI SCHEDULER STATUS\n\n"
        f"Date: {display_date(str(payload.get('card_date')))}\n"
        f"Today’s first pitch: {times.get('first_pitch_et')}\n"
        f"T-50 Verification: {verification.get('ready_for_image_generation', 'Not run')}\n"
        f"T-45 Generation: {generation.get('generated', 'Not run')}\n"
        f"T-43 Posting: {posting.get('status', 'Not run')}\n"
        f"Approval required: {'✅ Yes' if payload.get('approval_required') else '❌ No'}\n"
        f"Last scheduler error: {payload.get('last_scheduler_error', 'None')}"
    )


async def _process_three_step_pregame_workflow(bot: Any, day: str, current: datetime) -> None:
    """Run T-50/T-45/T-43 jobs exactly once when due."""
    times = schedule_times(day)
    first_pitch = times.get("first_pitch_et")
    if first_pitch is None:
        return
    log = _read_log()
    log.setdefault("scheduler", {})["last_checked_at"] = current.isoformat()
    log["scheduler_initialized"] = True
    app_timezone = get_app_timezone()
    log["timezone_used"] = str(app_timezone)
    log["first_pitch_et"] = first_pitch.isoformat()
    logging.info("Scheduler initialized")
    logging.info("Timezone used: %s", app_timezone)
    logging.info("First pitch ET: %s", first_pitch.isoformat())

    jobs = [
        ("pregame_verify", "Verification scheduled", times.get("verify_time"), pregame_verify_job),
        ("generate_cards", "Generation scheduled", times.get("generate_time"), generate_cards_job),
        ("post_cards", "Posting scheduled", times.get("post_time"), post_cards_job),
    ]
    for job_id, label, due_time, function in jobs:
        if due_time is None:
            continue
        logging.info("%s: %s", label, due_time.isoformat())
        entry = log.get(day, {}).get(job_id, {})
        if isinstance(entry, dict) and entry.get("status") in {"sent", "skipped", "blocked"}:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("status") == "waiting_for_approval"
            and os.getenv("AUTO_POST_APPROVED", "false").strip().lower() not in {"1", "true", "yes", "on"}
        ):
            continue
        if current < due_time:
            continue
        # Never post before T-43; this guard is deliberately explicit.
        if job_id == "post_cards" and current < times["post_time"]:
            continue
        try:
            result = await function(bot, day)
            status = (
                "waiting_for_approval"
                if isinstance(result, dict) and result.get("reason") == "approval_required"
                else "blocked"
                if isinstance(result, dict) and result.get("posted") is False and job_id == "post_cards"
                else "sent"
            )
            log = _read_log()
            log.setdefault(day, {})[job_id] = {
                "status": status,
                "recorded_at": now_et().isoformat(),
                "detail": result,
            }
            _write_log(log)
        except Exception as error:
            logging.exception("Scheduler job %s failed", job_id)
            log = _read_log()
            log["last_scheduler_error"] = f"{job_id}: {error}"
            log.setdefault(day, {})[job_id] = {
                "status": "failed",
                "recorded_at": now_et().isoformat(),
                "detail": str(error),
            }
            _write_log(log)


async def process_game_aware_posts(bot: Any, now: datetime | None = None) -> None:
    """Evaluate due pregame cards and end-of-day results exactly once."""
    current = (now or now_et()).astimezone(get_app_timezone())
    day = official_sports_date(current).isoformat()
    await _process_three_step_pregame_workflow(bot, day, current)
    # End-of-day results remain separate from the pregame workflow.
    await post_daily_results_if_ready(bot, now=current)


async def run_game_aware_scheduler(application: Any) -> None:
    """Run the pregame/results scheduler; errors never stop Telegram polling."""
    try:
        poll_seconds = max(30, int(os.getenv("SCHEDULER_POLL_SECONDS", "120")))
    except ValueError:
        logging.warning("Invalid SCHEDULER_POLL_SECONDS; using 120 seconds")
        poll_seconds = 120
    while True:
        try:
            await process_game_aware_posts(application.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Unexpected game-aware scheduler failure")
        await asyncio.sleep(poll_seconds)
