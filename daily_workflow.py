"""BETGPTAI 3-step MLB pregame workflow.

The workflow is intentionally conservative:

T-50: verify data and save a verification report.
T-45: generate cards/images and save previews.
T-43: post only if quality gates pass and approval allows it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_analysis import analyze_mlb_slate, get_last_analysis_metadata, upcoming_mlb_slate
from best_hit_prop_image import prepare_best_hit_prop_image
from card_time import official_sports_date
from elite_quant_engine import build_elite_quant_slate
from lineup_verification import invalidate_scratched_props, render_prop_scratch_alert, summarize_lineups
from mlb_auto_image import prepare_mlb_auto_image
from mlb_data import get_combined_slate, get_mlb_schedule
from model_report import save_model_report
from player_props_engine import build_player_props_lab
from core.builder import build_card_from_analysis
from core.card import structured_card_to_dict
from results_tracker import load_picks, save_official_picks
from safe_parlay_formatter import render_safe_parlay
from premium_card_formatter import (
    render_category_card,
    render_mlb_premium_card,
    render_play_of_day_card,
)
from services.pick_persistence import save_official_card as persist_official_card
from services.simple_mlb_card import (
    build_simple_mlb_card,
    export_simple_card_to_official_picks,
    render_simple_mlb_card,
    save_simple_mlb_card,
)
from storage import data_file, storage_status
from time_utils import format_et, now_et, to_et


WORKFLOW_VERSION = "elite_quant_v20"


def _live_mlb_engine() -> str:
    return os.getenv("LIVE_MLB_ENGINE", "simple").strip().lower()


def _live_mlb_engine_env_value() -> str:
    raw = os.getenv("LIVE_MLB_ENGINE")
    return raw.strip().lower() if raw is not None and raw.strip() else "not_set"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def workflow_dir(card_date: str) -> Path:
    """Return the workflow directory for one card date."""
    path = data_file("workflow") / card_date
    path.mkdir(parents=True, exist_ok=True)
    return path


def workflow_file(card_date: str, filename: str) -> Path:
    """Return one workflow file path."""
    return workflow_dir(card_date) / filename


def get_first_mlb_pitch(card_date: str) -> datetime | None:
    """Fetch today's MLB schedule and return the first pitch in app timezone."""
    schedule = get_mlb_schedule(card_date)
    valid_games = [
        game for game in schedule
        if not any(
            word in str(game.get("status", "")).lower()
            for word in ("cancelled", "canceled", "postponed")
        )
    ]
    times = [parsed for game in valid_games if (parsed := to_et(game.get("game_time"))) is not None]
    return min(times) if times else None


def schedule_times(card_date: str) -> dict[str, Any]:
    """Return first pitch and T-50/T-45/T-43 times."""
    error = None
    try:
        first_pitch = get_first_mlb_pitch(card_date)
    except Exception as exc:
        first_pitch = None
        error = str(exc)
    if first_pitch is None:
        return {
            "card_date": card_date,
            "first_pitch_et": None,
            "verify_time": None,
            "generate_time": None,
            "post_time": None,
            "error": error,
        }
    return {
        "card_date": card_date,
        "first_pitch_et": first_pitch,
        "verify_time": first_pitch - timedelta(minutes=50),
        "generate_time": first_pitch - timedelta(minutes=45),
        "post_time": first_pitch - timedelta(minutes=43),
    }


def _display_time_map(times: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_date": times.get("card_date"),
        "first_pitch_et": format_et(times.get("first_pitch_et")),
        "verify_time_et": format_et(times.get("verify_time")),
        "generate_time_et": format_et(times.get("generate_time")),
        "post_time_et": format_et(times.get("post_time")),
    }


def _posting_log() -> dict[str, Any]:
    path = data_file("posting_log.json")
    payload = _read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def _save_posting_log(payload: dict[str, Any]) -> None:
    _write_json(data_file("posting_log.json"), payload)


def workflow_status(card_date: str | None = None) -> dict[str, Any]:
    """Return persisted workflow status for owner commands."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    times = schedule_times(selected)
    log = _posting_log()
    verification = _read_json(workflow_file(selected, "pregame_verification.json"), {})
    generation = _read_json(workflow_file(selected, "generation_status.json"), {})
    posting = log.get(selected, {}).get("mlb_pregame_post", {})
    return {
        "version": WORKFLOW_VERSION,
        "card_date": selected,
        "times": _display_time_map(times),
        "verification": verification,
        "generation": generation,
        "posting": posting,
        "auto_post_enabled": _auto_post_enabled(),
        "approval_required": False,
        "last_scheduler_error": log.get("last_scheduler_error", "None"),
        "live_engine": "simple_mlb_card_v1" if _live_mlb_engine() == "simple" else "advanced",
        "live_mlb_engine_env_value": _live_mlb_engine_env_value(),
    }


def _saved_picks_for_date(card_date: str) -> list[dict[str, Any]]:
    """Return saved official MLB picks for one card date."""
    try:
        picks = load_picks()
    except Exception:
        return []
    return [
        pick for pick in picks
        if isinstance(pick, dict)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
        and str(pick.get("sport") or "mlb").lower() == "mlb"
        and pick.get("category") != "parlay_leg"
    ]


def _generation_flags(card: str, card_date: str, saved_picks: int, image_path: Any, best_hit_image: Any) -> dict[str, Any]:
    """Split T-45 card generation, image generation, and posting readiness."""
    picks = _saved_picks_for_date(card_date)
    has_saved_picks = saved_picks > 0 or bool(picks)
    has_play_of_day = any(
        str(pick.get("category") or "").lower() in {"play_of_day", "play of the day"}
        or str(pick.get("market_type") or "").lower() == "play_of_day"
        for pick in picks
    ) or "🔥 PLAY OF THE DAY" in card
    has_top_mlb = any(
        str(pick.get("market_type") or pick.get("pick_type") or "").lower()
        in {"moneyline", "runline", "f5_moneyline", "total", "team_total"}
        for pick in picks
    ) or any(marker in card for marker in ("TOP 5", "TOP 2", "TOP MLB PLAYS"))
    parlay_pick = next((pick for pick in picks if str(pick.get("market_type") or "").lower() == "parlay" or pick.get("category") == "parlay"), None)
    parlay_status = "generated" if parlay_pick else "no_qualified" if "No Safe 2-Leg Parlay qualified" in card else "not_found"
    card_complete = bool(card and has_play_of_day and has_top_mlb and has_saved_picks and parlay_status in {"generated", "no_qualified"})
    images_complete = bool(image_path or best_hit_image)
    return {
        "card_generation_complete": card_complete,
        "image_generation_complete": images_complete,
        "picks_saved": has_saved_picks,
        "play_of_day_generated": has_play_of_day,
        "top_mlb_plays_generated": has_top_mlb,
        "safe_parlay_status": parlay_status,
        "posting_ready": card_complete and has_saved_picks and _auto_post_enabled(),
    }


async def _save_official_picks_with_retry(
    analysis: str,
    slate: list[dict[str, Any]],
    card_date: str,
    source_command: str,
) -> dict[str, Any]:
    """Build StructuredCard and save official picks, retrying once before failing T-45.

    Returns a dict with workflow state fields including:
      saved_count, structured_card_built, official_picks_count,
      save_path_used, last_save_exception, generation_source_file,
      generation_source_function
    """
    last_save_exception = ""
    for attempt in (1, 2):
        try:
            card = build_card_from_analysis(analysis, slate, card_date, source_command)
            builder_count = len(card.official_picks)
            logging.info("TRACE build_card_from_analysis official_picks=%s card_date=%s", builder_count, card_date)
            card_dict = structured_card_to_dict(card)
            dict_count = len(card_dict.get("official_picks", []))
            logging.info("TRACE structured_card_to_dict official_picks=%s card_date=%s", dict_count, card_date)
            card_dict["analysis"] = analysis
            card_dict["slate"] = slate
            card_dict["source_command"] = source_command
            persist_count = len(card_dict.get("official_picks", []))
            logging.info("TRACE before persist_official_card official_picks=%s card_date=%s", persist_count, card_date)
            result = await asyncio.to_thread(persist_official_card, card_dict)
            if not result.get("success"):
                raise RuntimeError(str(result.get("error") or "Pick persistence failed"))
            saved = int(result.get("saved_pick_count") or 0)
            logging.info(
                "TRACE after persist_official_card success=%s saved=%s error=%s card_date=%s",
                result.get("success"), saved, result.get("error", ""), card_date,
            )
            logging.info(
                "Official picks saved card_date=%s saved=%s attempt=%s structured_card_built=true picks_count=%s",
                card_date, saved, attempt, len(card.official_picks),
            )
            return {
                "saved_count": saved,
                "structured_card_built": True,
                "official_picks_count": len(card.official_picks),
                "save_path_used": str(data_file("picks.json")),
                "last_save_exception": "",
                "generation_source_file": "daily_workflow.py",
                "generation_source_function": "_save_official_picks_with_retry",
            }
        except Exception as error:
            last_save_exception = str(error)
            logging.exception("Official picks save failed attempt=%s card_date=%s", attempt, card_date)
            if attempt == 1:
                await asyncio.sleep(0.5)
    raise RuntimeError(f"Could not save official picks after retry: {last_save_exception}")


def _auto_post_enabled() -> bool:
    """Master kill switch for automatic T-43 channel posting."""
    return os.getenv("AUTO_POST_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _auto_post_approved() -> bool:
    """Backward-compatible alias; AUTO_POST_ENABLED is now source of truth."""
    return _auto_post_enabled()


def _clear_stale_image_cache(card_date: str) -> int:
    """Remove stale image files outside today's date folders."""
    root = data_file("generated_cards")
    if not root.exists():
        return 0
    keep = {card_date, datetime.fromisoformat(card_date).strftime("%m-%d-%Y")}
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name in keep:
            continue
        for pattern in ("best_hit_prop*", "best_hit_art*", "mlb_auto_card*"):
            for file_path in folder.glob(pattern):
                if file_path.is_file():
                    file_path.unlink(missing_ok=True)
                    removed += 1
    return removed


def _clear_stale_prop_cache(card_date: str) -> dict[str, Any]:
    """Keep only today's date-keyed prop caches."""
    results: dict[str, Any] = {}
    for filename in ("props_lab.json", "approved_props.json", "best_hit_prop.json"):
        path = data_file(filename)
        payload = _read_json(path, {})
        if not isinstance(payload, dict):
            _write_json(path, {})
            results[filename] = "reset"
            continue
        if filename == "approved_props.json":
            cleaned = {
                key: value for key, value in payload.items()
                if isinstance(value, dict) and value.get("card_date") == card_date
            }
        else:
            cleaned = {card_date: payload[card_date]} if card_date in payload else {}
        _write_json(path, cleaned)
        results[filename] = len(payload) - len(cleaned)
    return results


async def pregame_verify_job(bot: Any, card_date: str | None = None) -> dict[str, Any]:
    """T-50 verification job."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    errors: list[str] = []
    noncritical_errors: list[str] = []
    schedule: list[dict[str, Any]] = []
    slate: list[dict[str, Any]] = []
    quant_payload: list[dict[str, Any]] = []
    try:
        schedule = await asyncio.to_thread(get_mlb_schedule, selected)
    except Exception as error:
        errors.append(f"Schedule failed: {error}")
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        slate = upcoming_mlb_slate(slate)
        quant_payload = await asyncio.to_thread(build_elite_quant_slate, slate, include_market=bool(os.getenv("ODDS_API_KEY", "")))
    except Exception as error:
        errors.append(f"Combined slate failed: {error}")
    storage = await asyncio.to_thread(storage_status)
    props_payload: dict[str, Any] = {}
    try:
        if slate:
            props_payload = await asyncio.to_thread(build_player_props_lab, slate, selected)
    except Exception as error:
        noncritical_errors.append(f"Props verification failed: {error}")
    stale_props = await asyncio.to_thread(_clear_stale_prop_cache, selected)
    stale_images = await asyncio.to_thread(_clear_stale_image_cache, selected)
    pitchers = sum(
        1 for game in schedule for key in ("away_pitcher", "home_pitcher")
        if game.get(key) and game.get(key) != "TBD"
    )
    weather_ok = any(isinstance(game.get("weather"), dict) for game in slate)
    lineup_summary = await asyncio.to_thread(summarize_lineups, slate, selected) if slate else {}
    confirmed_lineups = int(lineup_summary.get("confirmed_lineups") or 0)
    projected_lineups = int(lineup_summary.get("projected_lineups") or 0)
    waiting_lineups = int(lineup_summary.get("games_waiting") or 0)
    props_ok = bool(props_payload.get("all_props")) if isinstance(props_payload, dict) else False
    player_team_mapping = "checked" if props_payload else "not_required"
    odds_ok = any(game.get("best_available_prices") not in (None, "", "unavailable", [], {}) for game in slate)
    ready = bool(schedule and pitchers and weather_ok and odds_ok and storage.get("results_database_healthy") and not errors)
    report = {
        "version": WORKFLOW_VERSION,
        "card_date": selected,
        "created_at": now_et().isoformat(timespec="seconds"),
        "schedule_games": len(schedule),
        "lineups": "confirmed" if confirmed_lineups else "projected" if projected_lineups else "not_confirmed",
        "confirmed_lineups": confirmed_lineups,
        "projected_lineups": projected_lineups,
        "games_waiting_for_lineups": waiting_lineups,
        "pitchers_verified": pitchers,
        "weather": "available" if weather_ok else "unavailable",
        "odds": "available" if odds_ok else "unavailable",
        "injuries": "optional",
        "player_team_mapping": player_team_mapping,
        "props": "available" if props_ok else "unavailable",
        "stale_props_cleared": stale_props,
        "stale_images_removed": stale_images,
        "storage_healthy": bool(storage.get("results_database_healthy")),
        "ready_for_image_generation": ready,
        "critical_failures": errors,
        "noncritical_failures": noncritical_errors,
        "elite_quant_slate": quant_payload,
    }
    _write_json(workflow_file(selected, "pregame_verification.json"), report)
    await _notify_admin(
        bot,
        "✅ T-50 Verification Complete\n\n"
        f"Lineups: {report['lineups']}\n"
        f"Pitchers: {pitchers}\n"
        f"Weather: {report['weather']}\n"
        f"Odds: {report['odds']}\n"
        f"Props: {report['props']}\n"
        f"Ready for image generation: {'YES' if ready else 'NO'}\n\n"
        f"Critical failures: {', '.join(errors) if errors else 'None'}\n"
        f"Non-critical: {', '.join(noncritical_errors) if noncritical_errors else 'None'}",
    )
    return report


async def generate_cards_job(bot: Any, card_date: str | None = None) -> dict[str, Any]:
    """T-45 generation job. Sends admin previews only."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    if _live_mlb_engine() == "simple":
        try:
            simple_card = await asyncio.to_thread(build_simple_mlb_card, selected)
            simple_path = await asyncio.to_thread(save_simple_mlb_card, simple_card)
            bridge = await asyncio.to_thread(export_simple_card_to_official_picks, selected)
            all_picks = simple_card.get("all_picks") or simple_card.get("picks") or []
            saved_count = len(all_picks)
            if saved_count < 1:
                raise RuntimeError("Simple MLB card generated zero picks.")
            parlay_legs = simple_card.get("parlay") or []
            status = {
                "version": WORKFLOW_VERSION,
                "card_date": selected,
                "created_at": now_et().isoformat(timespec="seconds"),
                "generated": True,
                "card_generation_complete": True,
                "simple_generate": True,
                "advanced_generate": "skipped",
                "live_engine": "simple_mlb_card_v1",
                "live_mlb_engine_env_value": _live_mlb_engine_env_value(),
                "image_generation_complete": False,
                "posting_ready": bool(_auto_post_enabled()),
                "picks_saved": True,
                "saved_picks": saved_count,
                "saved_count": saved_count,
                "safe_parlay_found": bool(parlay_legs),
                "safe_parlay_status": "generated" if parlay_legs else "no_qualified",
                "simple_card_path": simple_path,
                "bridge_imported": int(bridge.get("imported") or 0),
                "structured_card_built": False,
                "official_picks_count": saved_count,
                "last_generation_error": None,
                "generation_error": None,
                "last_save_exception": "",
                "errors": [],
                "generation_source_file": "services/simple_mlb_card.py",
                "generation_source_function": "build_simple_mlb_card",
            }
            _write_json(workflow_file(selected, "generation_status.json"), status)
            await _notify_admin(bot, f"✅ T-45 Simple Generate Complete\nPicks: {saved_count}\nLive Engine: Simple MLB Card v1")
            return status
        except Exception as error:
            logging.exception("T-45 simple generation failed")
            status = {
                "version": WORKFLOW_VERSION, "card_date": selected,
                "created_at": now_et().isoformat(timespec="seconds"),
                "generated": False, "card_generation_complete": False,
                "simple_generate": False, "advanced_generate": "skipped",
                "live_engine": "simple_mlb_card_v1",
                "live_mlb_engine_env_value": _live_mlb_engine_env_value(),
                "picks_saved": False, "saved_picks": 0,
                "last_generation_error": str(error), "generation_error": str(error),
                "errors": [str(error)],
            }
            _write_json(workflow_file(selected, "generation_status.json"), status)
            await _notify_admin(bot, f"❌ T-45 Simple Generate failed: {error}")
            return status
    output_dir = data_file("generated_cards") / selected
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    slate: list[dict[str, Any]] = []
    card = ""
    saved_picks = 0
    save_result: dict[str, Any] = {}
    image_path = None
    best_hit_image = None
    generation_error = ""
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        slate = upcoming_mlb_slate(slate)
        if not slate:
            raise RuntimeError("No upcoming MLB games available for generation.")
        card = await analyze_mlb_slate(
            slate,
            os.getenv("OPENAI_API_KEY", ""),
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        await asyncio.to_thread(save_model_report, selected, slate, card, get_last_analysis_metadata())
        save_result = await _save_official_picks_with_retry(card, slate, selected, "scheduled_generate")
        saved_picks = int(save_result.get("saved_count") or 0)
        try:
            image_result = await asyncio.to_thread(
                prepare_mlb_auto_image,
                card,
                selected,
                image_generation_enabled=os.getenv("IMAGE_GENERATION_ENABLED", "").lower() in {"1", "true", "yes", "on"},
            )
            image_path = image_result.get("image_path") or image_result.get("prompt_path")
        except Exception as image_error:
            logging.exception("T-45 MLB image generation failed; continuing with text card")
            errors.append(f"MLB image failed: {image_error}")
        try:
            best_hit_result = await asyncio.to_thread(
                prepare_best_hit_prop_image,
                slate,
                selected,
                image_generation_enabled=os.getenv("IMAGE_GENERATION_ENABLED", "").lower() in {"1", "true", "yes", "on"},
            )
            best_hit_image = best_hit_result.get("image_path") or best_hit_result.get("prompt_path")
        except Exception as image_error:
            logging.exception("T-45 best hit image generation failed; continuing with text fallback")
            errors.append(f"Best hit image failed: {image_error}")
        scratched = await asyncio.to_thread(invalidate_scratched_props, selected, slate)
        if scratched.get("invalidated"):
            alert_text = render_prop_scratch_alert(scratched)
            await _notify_admin(
                bot,
                alert_text,
            )
    except Exception as error:
        logging.exception("T-45 generation failed")
        generation_error = str(error)
        errors.append(generation_error)
    flags = _generation_flags(card, selected, saved_picks, image_path, best_hit_image)
    if errors and not generation_error:
        generation_error = "; ".join(errors)
    status = {
        "version": WORKFLOW_VERSION,
        "card_date": selected,
        "created_at": now_et().isoformat(timespec="seconds"),
        "generated": flags["card_generation_complete"],
        "card_generation_complete": flags["card_generation_complete"],
        "image_generation_complete": flags["image_generation_complete"],
        "posting_ready": flags["posting_ready"],
        "picks_saved": flags["picks_saved"],
        "play_of_day_generated": flags["play_of_day_generated"],
        "top_mlb_plays_generated": flags["top_mlb_plays_generated"],
        "safe_parlay_status": flags["safe_parlay_status"],
        "saved_picks": saved_picks,
        "mlb_card_path": str(output_dir / "mlb_card.txt"),
        "mlb_image": image_path,
        "best_hit_image": best_hit_image,
        "generation_error": generation_error,
        "errors": errors,
        "structured_card_built": bool(save_result.get("structured_card_built", False)),
        "official_picks_count": int(save_result.get("official_picks_count", 0)),
        "save_path_used": str(save_result.get("save_path_used", "")),
        "last_save_exception": str(save_result.get("last_save_exception", "")),
        "generation_source_file": str(save_result.get("generation_source_file", "")),
        "generation_source_function": str(save_result.get("generation_source_function", "")),
        "simple_generate": False,
        "advanced_generate": "failed" if generation_error else "testing",
        "live_engine": "advanced",
        "live_mlb_engine_env_value": _live_mlb_engine_env_value(),
        "last_generation_error": generation_error or None,
    }
    if card:
        (output_dir / "mlb_card.txt").write_text(card, encoding="utf-8")
    _write_json(workflow_file(selected, "generation_status.json"), status)
    await _notify_admin(
        bot,
        "✅ T-45 Generation Complete\n\n"
        f"Card generated: {'YES' if status['card_generation_complete'] else 'NO'}\n"
        f"Images generated: {'YES' if status['image_generation_complete'] else 'NO — text fallback available'}\n"
        f"Picks saved: {saved_picks}\n"
        f"Safe parlay: {status['safe_parlay_status']}\n"
        f"MLB image/prompt: {image_path or 'Unavailable'}\n"
        f"Best hit image/prompt: {best_hit_image or 'Unavailable'}\n"
        f"Auto-post enabled: {'YES' if _auto_post_enabled() else 'NO — AUTO_POST_ENABLED=false'}\n"
        f"Last generation error: {generation_error or 'None'}",
    )
    return status


async def post_cards_job(bot: Any, card_date: str | None = None) -> dict[str, Any]:
    """T-43 posting job guarded by quality gate and approval state."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    log = _posting_log()
    posted_key = f"posted_mlb_card_{selected}"
    if _live_mlb_engine() == "simple":
        if log.get(posted_key) or log.get(selected, {}).get("simple_card_posted"):
            return {"posted": False, "posted_today": True, "simple_card_posted": True, "reason": "already_posted"}
        if not _auto_post_enabled():
            return {"posted": False, "posted_today": False, "simple_card_posted": False, "reason": "auto_post_disabled"}
        path = data_file("simple_cards") / f"{selected}.json"
        try:
            simple_card = _read_json(path, {}) if path.exists() else {}
            if not isinstance(simple_card, dict) or not (simple_card.get("all_picks") or simple_card.get("picks")):
                simple_card = await asyncio.to_thread(build_simple_mlb_card, selected)
                await asyncio.to_thread(save_simple_mlb_card, simple_card)
                await asyncio.to_thread(export_simple_card_to_official_picks, selected)
            pick_count = len(simple_card.get("all_picks") or simple_card.get("picks") or [])
            if pick_count < 1:
                raise RuntimeError("Simple MLB card has no picks to post.")
            chat = os.getenv("FREE_CHANNEL_ID", "").strip()
            if not chat:
                raise RuntimeError("FREE_CHANNEL_ID is not configured.")
            await _send_long(bot, _destination(chat), render_simple_mlb_card(simple_card))
            log[posted_key] = True
            log.setdefault(selected, {})["simple_card_posted"] = True
            log[selected]["mlb_pregame_post"] = {
                "status": "sent", "engine": "simple_mlb_card_v1",
                "recorded_at": now_et().isoformat(timespec="seconds"),
            }
            _save_posting_log(log)
            return {"posted": True, "posted_today": True, "simple_card_posted": True, "live_engine": "simple_mlb_card_v1"}
        except Exception as error:
            logging.exception("T-43 simple post failed")
            log.setdefault(selected, {})["mlb_pregame_post"] = {"status": "failed", "reason": str(error)}
            _save_posting_log(log)
            return {"posted": False, "posted_today": False, "simple_card_posted": False, "reason": str(error)}
    if log.get(posted_key):
        return {"posted": False, "reason": "already_posted"}
    if not _auto_post_enabled():
        log.setdefault(selected, {})["mlb_pregame_post"] = {
            "status": "disabled",
            "recorded_at": now_et().isoformat(timespec="seconds"),
            "reason": "AUTO_POST_ENABLED=false",
        }
        _save_posting_log(log)
        await _notify_admin(bot, "⏸ T-43 Posting skipped. AUTO_POST_ENABLED=false.")
        return {"posted": False, "reason": "auto_post_disabled"}
    verification = _read_json(workflow_file(selected, "pregame_verification.json"), {})
    generation = _read_json(workflow_file(selected, "generation_status.json"), {})
    if not generation.get("card_generation_complete"):
        log.setdefault(selected, {})["mlb_pregame_post"] = {
            "status": "blocked",
            "recorded_at": now_et().isoformat(timespec="seconds"),
            "reason": "card_generation_not_completed",
        }
        _save_posting_log(log)
        await _notify_admin(bot, "❌ T-43 Posting blocked: T-45 card generation is not complete.")
        return {"posted": False, "reason": "card_generation_not_completed", "generation": generation}
    if not generation.get("picks_saved"):
        log.setdefault(selected, {})["mlb_pregame_post"] = {
            "status": "blocked",
            "recorded_at": now_et().isoformat(timespec="seconds"),
            "reason": "picks_not_saved",
        }
        _save_posting_log(log)
        await _notify_admin(bot, "❌ T-43 Posting blocked: official picks were not saved to picks.json.")
        return {"posted": False, "reason": "picks_not_saved", "generation": generation}
    picks_file = data_file("picks.json")
    todays_picks = _saved_picks_for_date(selected)
    if not picks_file.exists() or not todays_picks:
        log.setdefault(selected, {})["mlb_pregame_post"] = {
            "status": "blocked",
            "recorded_at": now_et().isoformat(timespec="seconds"),
            "reason": "no_saved_picks_for_today",
        }
        _save_posting_log(log)
        await _notify_admin(
            bot,
            "❌ T-43 Posting blocked: picks.json does not contain today's official picks.",
        )
        return {"posted": False, "reason": "no_saved_picks_for_today"}
    card_path = data_file("generated_cards") / selected / "mlb_card.txt"
    card = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    if not card:
        await _notify_admin(bot, "❌ T-43 Posting blocked: generated MLB card is missing.")
        return {"posted": False, "reason": "missing_card"}
    posts = [
        ("FREE_CHANNEL_ID", _free_channel_posts(card, selected)),
        ("VIP_CHANNEL_ID", _vip_channel_posts(card, selected)),
    ]
    sent = []
    for env_name, messages in posts:
        chat = os.getenv(env_name, "").strip()
        if not chat:
            continue
        try:
            for content in messages:
                await _send_long(bot, _destination(chat), content)
            sent.append(env_name)
        except Exception as error:
            logging.exception("T-43 post failed for %s", env_name)
            await _notify_admin(bot, f"❌ T-43 post failed for {env_name}: {error}")
    if not sent:
        log.setdefault(selected, {})["mlb_pregame_post"] = {
            "status": "blocked",
            "recorded_at": now_et().isoformat(timespec="seconds"),
            "reason": "no_destinations_sent",
        }
        _save_posting_log(log)
        await _notify_admin(bot, "❌ T-43 Posting blocked: no FREE_CHANNEL_ID or VIP_CHANNEL_ID destination accepted posts.")
        return {"posted": False, "reason": "no_destinations_sent"}
    log[posted_key] = True
    log.setdefault(selected, {})["mlb_pregame_post"] = {
        "status": "sent",
        "recorded_at": now_et().isoformat(timespec="seconds"),
        "sent": sent,
    }
    _save_posting_log(log)
    await _notify_admin(bot, f"✅ T-43 Posting Complete\nSent: {', '.join(sent) if sent else 'No destinations configured'}")
    return {"posted": True, "sent": sent, "verification": verification, "generation": generation}


async def force_post_free_channel_job(bot: Any, card_date: str | None = None) -> dict[str, Any]:
    """Owner-only manual free-channel text post that does not require images."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    log = _posting_log()
    posted_key = f"posted_free_mlb_card_{selected}"
    if log.get(posted_key):
        return {"posted": False, "reason": "already_posted_free"}
    if not _auto_post_enabled():
        return {"posted": False, "reason": "auto_post_disabled"}
    picks = _saved_picks_for_date(selected)
    if not picks:
        return {"posted": False, "reason": "no_saved_picks"}
    chat = os.getenv("FREE_CHANNEL_ID", "").strip()
    if not chat:
        return {"posted": False, "reason": "missing_free_channel_id"}
    card_path = data_file("generated_cards") / selected / "mlb_card.txt"
    card = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    messages = _free_channel_posts(card, selected)
    for content in messages:
        await _send_long(bot, _destination(chat), content)
    log[posted_key] = True
    log.setdefault(selected, {})["free_mlb_manual_post"] = {
        "status": "sent",
        "recorded_at": now_et().isoformat(timespec="seconds"),
        "sent": ["FREE_CHANNEL_ID"],
    }
    _save_posting_log(log)
    await _notify_admin(bot, "✅ Force post complete: FREE_CHANNEL_ID received today’s free MLB card.")
    return {"posted": True, "sent": ["FREE_CHANNEL_ID"]}


def _section(card: str, heading: str) -> str:
    start = card.find(heading)
    if start < 0:
        return ""
    end = len(card)
    for marker in (
        "🔥 PLAY OF THE DAY",
        "🏆 TOP 5 MONEYLINE",
        "🔥 TOP 5 F5",
        "📈 TOP 5 RUN LINE",
        "🎯 TOP 5 GAME TOTALS",
        "💰 TOP 5 TEAM TOTALS",
        "🧩 2-LEG SAFE PARLAY",
        "💎 WANT THE FULL BETGPTAI",
    ):
        if marker == heading:
            continue
        pos = card.find(marker, start + len(heading))
        if pos >= 0:
            end = min(end, pos)
    return card[start:end].strip().strip("━").strip()


def _clean_channel_section(section: str, *, keep_reason: bool = True) -> str:
    """Remove crowded app/footer text from channel posts."""
    if not section:
        return ""
    blocked_phrases = (
        "BETGPTAI NOTE",
        "BETGPTAI RECOMMENDATION",
        "WANT THE FULL BETGPTAI",
        "Premium includes",
        "Type /vip",
        "Past performance",
        "Card timing follows",
        "All game times are listed",
        "Odds vary by sportsbook",
        "Please shop",
        "Singles are recommended",
        "Parlays are optional",
        "Educational analysis only",
        "Value Note",
    )
    cleaned: list[str] = []
    skip_block = False
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if any(phrase.lower() in line.lower() for phrase in blocked_phrases):
            skip_block = True
            continue
        if skip_block:
            # Resume once a numbered/check pick line or major heading appears.
            if re.match(r"^(?:[1-5]️⃣|✅|🔥|⚾|🏆|📈|🎯|💰)", line):
                skip_block = False
            else:
                continue
        if not keep_reason and line.lower().startswith("reason:"):
            continue
        cleaned.append(raw_line.rstrip())
    text = "\n".join(cleaned).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _footer() -> str:
    return "Educational analysis only. Singles first. Parlays carry higher risk."


def _best_hit_text(card_date: str) -> str:
    if os.getenv("PROP_PUBLIC_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return "⚾ BEST HIT PROP\n\nPublic player props are disabled pending lineup verification."
    payload = _read_json(data_file("best_hit_prop.json"), {})
    prop = None
    if isinstance(payload, dict):
        day = payload.get(card_date)
        if isinstance(day, dict):
            prop = day.get("prop")
    if not isinstance(prop, dict):
        return "⚾ BEST HIT PROP\n\nBest Hit Prop is pending final verification."
    lineup = prop.get("lineup_verification") if isinstance(prop.get("lineup_verification"), dict) else {}
    lab = _read_json(data_file("props_lab.json"), {})
    day = lab.get(card_date) if isinstance(lab, dict) and isinstance(lab.get(card_date), dict) else {}
    cached_id = str(prop.get("prop_id") or "")
    cached_player = str(prop.get("player_name") or "").strip().lower()
    lab_match = next((item for item in day.get("all_props", []) if isinstance(item, dict) and (
        (cached_id and str(item.get("prop_id") or "") == cached_id)
        or (cached_player and str(item.get("player_name") or "").strip().lower() == cached_player and item.get("prop_type") == "hits")
    )), None)
    official_hits = day.get("official_hit_props") if isinstance(day.get("official_hit_props"), list) else []
    official_shape_ok = bool(official_hits) and all(
        item.get("prop_type") == "hits" and item.get("selection") == "Over"
        and item.get("line") == 0.5 and item.get("line_verified")
        and item.get("odds_verified") and item.get("over_odds") is not None
        and ((item.get("edge_score") is not None and item.get("edge_score") > 0)
             or float(item.get("model_score") or item.get("raw_score") or 0) >= 70)
        and isinstance(item.get("lineup_verification"), dict)
        and item["lineup_verification"].get("verified")
        and item["lineup_verification"].get("state") == "Confirmed"
        and isinstance(item.get("player_verification"), dict)
        and item["player_verification"].get("verified")
        and item["player_verification"].get("active_roster", True)
        and item.get("status") != "invalidated"
        for item in official_hits if isinstance(item, dict)
    )
    scratch_passed = (
        isinstance(lab_match, dict) and lab_match.get("status") != "invalidated"
        and bool(lab_match.get("line_verified")) and bool(lab_match.get("odds_verified"))
    )
    if not official_shape_ok or not lineup.get("verified") or lineup.get("state") == "Scratched" or prop.get("status") == "invalidated" or not scratch_passed:
        return "⚾ BEST HIT PROP\n\nBest Hit Prop is pending verified lineup and scratch checks."
    return (
        "⚾ BEST HIT PROP\n\n"
        f"👤 {prop.get('player_name', 'Player')}\n"
        f"🧢 {prop.get('team_name', 'Team')}\n"
        f"🆚 {prop.get('opponent_name', 'Opponent')}\n"
        f"🕒 {prop.get('game_time_et', 'Time unavailable ET')}\n\n"
        f"🎯 Prop: Over {prop.get('line', 0.5)} Hits"
    )


def _safe_parlay_text(card_date: str) -> str:
    """Render the saved official parlay legs in the premium shared format."""
    picks = _read_json(data_file("picks.json"), [])
    if not isinstance(picks, list):
        return "No Safe 2-Leg Parlay qualified today."
    parlay = next(
        (
            pick for pick in reversed(picks)
            if isinstance(pick, dict)
            and str(pick.get("card_date") or pick.get("date") or "") == card_date
            and pick.get("category") == "parlay"
        ),
        None,
    )
    legs = parlay.get("legs") if isinstance(parlay, dict) else []
    return render_safe_parlay(legs if isinstance(legs, list) else [], card_date=card_date)


def _top_mlb_plays(card: str) -> str:
    sections = [
        _section(card, "🏆 TOP 5 MONEYLINE"),
        _section(card, "🔥 TOP 5 F5"),
        _section(card, "📈 TOP 5 RUN LINE"),
        _section(card, "🎯 TOP 5 GAME TOTALS"),
        _section(card, "💰 TOP 5 TEAM TOTALS"),
    ]
    body = "\n\n━━━━━━━━━━━━\n\n".join(
        _clean_channel_section(section) for section in sections if section
    )
    return body or "Top MLB plays are pending."


def _free_channel_posts(card: str, card_date: str) -> list[str]:
    best_hit = _best_hit_text(card_date)
    parlay = _safe_parlay_text(card_date)
    return [
        render_play_of_day_card(card_date),
        render_mlb_premium_card(card_date),
        f"{best_hit}\n\n━━━━━━━━━━━━\n\n{_footer()}",
        f"{parlay}\n\nTap /vip for the full BETGPTAI card.",
    ]


def _vip_channel_posts(card: str, card_date: str) -> list[str]:
    vault = _safe_parlay_text(card_date)
    return [
        render_mlb_premium_card(card_date),
        render_category_card(card_date, "🏆 TOP 5 MONEYLINE", "moneyline"),
        render_category_card(card_date, "📈 TOP 5 RUNLINE", "runline"),
        render_category_card(card_date, "🔥 TOP 5 F5 MONEYLINE", "f5_moneyline"),
        render_category_card(card_date, "🎯 TOP 5 TOTALS", "total"),
        render_category_card(card_date, "💰 TOP 5 TEAM TOTALS", "team_total"),
        f"⚾ FULL PROP CARD\n\n{_best_hit_text(card_date)}\n\nMore verified props pending admin approval.\n\n━━━━━━━━━━━━\n\n{_footer()}",
        f"🔥 BETGPTAI VIP VAULT\n\n{vault}\n\n━━━━━━━━━━━━\n\n{_footer()}",
    ]


def _community_teaser(card: str) -> str:
    play = _section(card, "🔥 PLAY OF THE DAY") or "🔥 PLAY OF THE DAY\n\nPending."
    return (
        "⚾ BETGPTAI COMMUNITY TEASER\n\n"
        f"{play}\n\n"
        "Full card available in the bot/VIP."
    )


def _destination(value: str) -> int | str:
    if value.startswith("@"):
        return value
    numeric = int(value)
    return -numeric if numeric > 0 and value.startswith("100") else numeric


async def _send_long(bot: Any, chat_id: int | str, text: str) -> None:
    remaining = text.strip()
    while remaining:
        if len(remaining) <= 3900:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n\n", 0, 3900)
            if split_at < 1:
                split_at = 3900
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip()
        await bot.send_message(chat_id=chat_id, text=chunk)


async def _notify_admin(bot: Any, text: str) -> None:
    admin_id = os.getenv("MY_TELEGRAM_ID", "594425739").strip() or "594425739"
    try:
        await _send_long(bot, int(admin_id), text)
    except Exception:
        logging.exception("Could not notify admin")


def seconds_until_next_job(card_date: str | None = None) -> int | None:
    selected = card_date or official_sports_date(now_et()).isoformat()
    times = schedule_times(selected)
    current = now_et()
    future = [
        value for key in ("verify_time", "generate_time", "post_time")
        if (value := times.get(key)) is not None and value > current
    ]
    if not future:
        return None
    return max(0, int((min(future) - current).total_seconds()))


def time_debug_payload(card_date: str | None = None) -> dict[str, Any]:
    selected = card_date or official_sports_date(now_et()).isoformat()
    times = schedule_times(selected)
    return {
        "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "et_now": now_et().isoformat(timespec="seconds"),
        "server_tz_env": os.getenv("TZ", "Not set"),
        "app_timezone": os.getenv("APP_TIMEZONE", "America/New_York"),
        **_display_time_map(times),
        "scheduler_running": True,
        "jobs_registered": ["pregame_verify_job", "generate_cards_job", "post_cards_job"],
        "seconds_until_next_job": seconds_until_next_job(selected),
    }
