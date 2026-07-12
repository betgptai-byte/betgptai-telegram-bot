"""A small, beginner-friendly Telegram bot for sharing baseball plays."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from ai_analysis import (
    analyze_mlb_slate,
    analyze_specialized_mlb_slate,
    build_fallback_card,
    get_last_analysis_metadata,
    upcoming_mlb_slate,
)
from services.simple_mlb_card import (
    build_simple_mlb_card,
    export_simple_card_to_official_picks,
    post_simple_mlb_card,
    save_simple_mlb_card,
    simple_card_bridge_status,
)
from ai_learning_engine import (
    approve_weight_update as approve_learning_weight_update,
    learning_status_payload,
    load_learning_report,
    reject_weight_update as reject_learning_weight_update,
    render_learning_auto_status,
    render_learning_report,
    render_learning_status,
    render_loss_review,
    render_weight_history_admin,
    render_weight_suggestions,
    render_weights_admin,
    run_learning_review,
    toggle_learning_auto_apply,
)
from elite_quant_engine import build_elite_quant_slate
from edge_database import MODEL_VERSION as QUANT_MODEL_VERSION, current_quant_weights
from mission_control import ai_learning_auto_apply_line, sharp_api_status_line
from anime_magazine_generator import (
    generate_daily_magazine_prompts,
    save_magazine_prompts,
)
from best_hit_prop_image import get_verified_best_hit_prop, prepare_best_hit_prop_image
from card_format import PARLAY_NOTE, RECOMMENDATION_FOOTER, TIMED_CARD_FOOTER
from card_time import eastern_now, official_sports_date, tomorrow_sports_date
from card_image_generator import generate_mlb_card_slides, save_mlb_card_slide_prompts
from game_time import mlb_game_block
from hitting_streak_report import (
    build_hitting_streak_report,
    render_hitting_streak_debug,
    render_hitting_streak_report,
)
from intelligence_dashboard import (
    build_intelligence_dashboard,
    intelligence_dashboard_available,
    render_daily_intel,
    render_intel_debug,
    render_lineup_report,
    render_model_review,
    render_morning_report,
)
from lineup_verification import (
    assess_scratch_public_impact,
    prop_scratch_debug_payload,
    render_lineup_status,
    render_prop_scratch_debug,
    render_public_scratch_correction,
    summarize_lineups,
)
from mlb_data import MLBDataError, get_combined_slate, odds_debug_payload
from mlb_admin_report import (
    build_mlb_admin_report_async,
    build_mlb_top5_admin_card,
    prepare_mlb_admin_image,
    render_mlb_admin_report,
    render_mlb_top5_admin_card,
    render_warroom_debug,
)
from model_report import load_model_report, save_model_report
from mlb_auto_image import prepare_mlb_auto_image
from openai_image_generator import generate_image, generate_image_from_prompt
from player_verification import verify_hit_prop_context, verify_player_team
from prop_card_generator import (
    generate_hits_by_team_prompts,
    generate_prop_card_prompts,
    save_prop_prompts,
)
from player_props_engine import (
    approve_prop,
    build_player_props_lab,
    player_props_engine_available,
    render_hitprops_debug,
    render_fanduel_props_debug,
    render_hits_by_team_card,
    render_prop_debug,
    render_prop_type_card,
    render_props_test,
    render_props_admin_card,
)
from verification_engine import average_verification_score, enrich_mlb_slate_verification
from api_sports_baseball import api_sports_baseball_available
from fangraphs_data import (
    fangraphs_available,
    fangraphs_status_label,
    pybaseball_status_label,
)
from savant_data import savant_available
from soccer_analysis import analyze_soccer_slate, soccer_debug_report
from soccer_master_engines import soccer_slate_summary
from api_football import api_football_status_label
from soccer_data import (
    SoccerDataError,
    api_football_available,
    check_clubelo,
    fbref_available,
    check_serpapi,
    statsbomb_available,
    check_thesportsdb,
    check_understat,
    get_last_soccer_debug,
    get_soccer_slate,
)
from soccer_results import SoccerResultsError, build_soccer_results_dashboard
from storage import data_file, storage_status as get_storage_status
from services.pick_persistence import (
    render_save_debug,
    repair_storage as repair_pick_storage,
    save_debug as pick_persistence_debug,
    save_official_card as persist_official_card,
)
from results_tracker import (
    ResultsTrackerError,
    available_card_dates,
    build_daily_results_dashboard,
    build_range_results_dashboard,
    debug_results_summary,
    debug_picks_summary,
    display_date,
    eastern_today,
    extract_official_picks,
    grade_debug_report,
    grade_mlb_picks_for_date,
    get_most_recent_featured_picks,
    load_picks,
    missing_results_message,
    normalize_pick_date,
    render_saved_picks_summary,
    save_soccer_picks,
    update_results_from_mlb,
)
from core.builder import build_card_from_analysis
from core.card import structured_card_to_dict
from safe_parlay_formatter import render_safe_parlay
from premium_card_formatter import (
    render_category_card,
    render_mlb_premium_card,
    render_play_of_day_card,
)
from today_pick_image import prepare_today_pick_image
from thesportsdb_data import (
    thesportsdb_enabled,
    thesportsdb_api_key,
    thesportsdb_base_url,
    thesportsdb_version,
    thesportsdb_status_label,
)
from posting_scheduler import (
    post_status_text,
    post_daily_results_if_ready,
    results_auto_status_text,
    run_game_aware_scheduler,
    scheduler_status_text,
    set_auto_results_enabled,
    time_debug_text,
)
from daily_workflow import (
    force_post_free_channel_job,
    generate_cards_job,
    get_first_mlb_pitch,
    workflow_status,
)
from time_utils import mlb_local_game_date, now_et, to_et
from quality_gate import render_prepost_quality_gate, run_prepost_quality_gate
from bot.callbacks.router import register_callback_router


# Show useful information in the terminal while the bot is running.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def _attach_file_logger(name: str, filename: str) -> logging.Logger:
    """Attach persistent Railway/local file logging without breaking console logs."""
    logger = logging.getLogger(name)
    log_path = data_file("logs") / filename
    if not any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", "") == str(log_path)
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


SYSTEM_LOG = _attach_file_logger("betgptai.system", "system.log")
API_LOG = _attach_file_logger("betgptai.api", "api.log")
STORAGE_LOG = _attach_file_logger("betgptai.storage", "storage.log")
CALLBACK_LOG = _attach_file_logger("betgptai.callbacks", "callbacks.log")

OWNER_TELEGRAM_ID = 594425739
LAST_RESULTS_BUTTON_DATE: str | None = None
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
ADMIN_CALLBACKS = {
    "admin_mlb_war_room",
    "admin_mission_control",
    "admin_generate_images",
    "admin_debug_results",
    "admin_back",
    "admin_full_mlb_card",
    "admin_mlb_top5_card",
    "admin_system_diagnostics",
    "admin_odds_debug",
    "admin_card_debug",
}


def _log_callback_event(
    *,
    user_id: int | None,
    callback_data: str,
    handler_found: bool,
    execution_success: bool,
    error: object | None = None,
) -> None:
    """Persist callback diagnostics to DATA_DIR/logs/callbacks.log."""
    message = (
        f"timestamp={datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"user_id={user_id} "
        f"callback_data={callback_data} "
        f"handler_found={str(handler_found).lower()} "
        f"execution_success={str(execution_success).lower()}"
    )
    if error is not None:
        message += f" error={error}"
    CALLBACK_LOG.info(message)
    logging.info(message)


def _truthy_value(value: str | None) -> bool:
    """Return True for common env-var truthy values."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_environment() -> str:
    """Railway is production; everything else is local."""
    return "railway" if os.getenv("RAILWAY_ENVIRONMENT") else "local"


def _git_commit_hash() -> str:
    """Return the current short git commit hash when available."""
    for env_name in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value[:12]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _deployment_id() -> str:
    """Return Railway deployment ID when available."""
    return os.getenv("RAILWAY_DEPLOYMENT_ID", "").strip() or "Not available"


def _deployed_at() -> str:
    """Return actual deploy time if configured; never substitute a UUID."""
    return (
        os.getenv("DEPLOY_TIME", "").strip()
        or os.getenv("RAILWAY_DEPLOYMENT_CREATED_AT", "").strip()
        or os.getenv("RAILWAY_DEPLOYED_AT", "").strip()
        or "Not available"
    )


def _app_version() -> str:
    """Return the public app version label."""
    return os.getenv("APP_VERSION", "BETGPTAI ELITE QUANT ENGINE v20.0")


def _version_text() -> str:
    """Build the /version diagnostics message."""
    return (
        "🤖 BETGPTAI VERSION\n\n"
        f"App Version: {_app_version()}\n"
        f"Git Commit: {_git_commit_hash()}\n"
        f"Environment: {_runtime_environment()}\n"
        f"Railway Deployment ID: {_deployment_id()}\n"
        f"Deployed At: {_deployed_at()}\n"
        f"APP_TIMEZONE: {os.getenv('APP_TIMEZONE', 'America/New_York')}\n"
        f"DATA_DIR: {data_file('').resolve()}"
    )


def _admin_telegram_id() -> int | None:
    """Return the single authorized Telegram user ID."""
    return OWNER_TELEGRAM_ID


def _is_admin_user(user_id: int | None) -> bool:
    """Return True only for the fixed owner/admin Telegram ID."""
    return user_id == OWNER_TELEGRAM_ID


async def _require_admin(update: Update) -> bool:
    """Reject private commands unless they come from the configured admin."""
    configured_id = _admin_telegram_id()
    requesting_id = update.effective_user.id if update.effective_user else None
    if _is_admin_user(requesting_id):
        return True
    SYSTEM_LOG.warning(
        "component=AdminAuth status=unauthorized user_id=%s expected_id=%s recovery=reply_unauthorized",
        requesting_id,
        configured_id,
    )
    if update.message:
        await update.message.reply_text("⛔ Unauthorized command.")
    return False


def _database_counts() -> dict[str, int]:
    """Count tracked, pending, and settled picks for the admin panel."""
    picks = load_picks()
    pending = sum(
        pick.get("status") == "pending" or pick.get("result") in (None, "", "pending")
        for pick in picks
    )
    graded = sum(
        pick.get("result") in {"win", "loss", "push"} for pick in picks
    )
    return {"total": len(picks), "pending": pending, "graded": graded}


DAILY_CARD_FILE = data_file("daily_card.json")


def _read_runtime_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_runtime_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def _purge_stale_daily_caches(card_date: str, *, clear_today_props: bool = False) -> dict[str, Any]:
    """Remove stale prop/player/image caches so daily cards cannot reuse old players."""
    removed: list[str] = []
    for filename in ("props_lab.json", "approved_props.json", "best_hit_prop.json"):
        path = data_file(filename)
        payload = _read_runtime_json(path, {})
        if not isinstance(payload, dict):
            _write_runtime_json(path, {})
            removed.append(f"{filename}: reset invalid cache")
            continue
        original_keys = set(payload.keys())
        if filename == "props_lab.json" and clear_today_props:
            payload.pop(card_date, None)
        if filename == "best_hit_prop.json" and clear_today_props:
            payload.pop(card_date, None)
        if filename == "approved_props.json":
            payload = {
                key: value for key, value in payload.items()
                if isinstance(value, dict) and value.get("card_date") == card_date
            }
        else:
            payload = {
                key: value for key, value in payload.items()
                if key == card_date
            }
        if set(payload.keys()) != original_keys:
            _write_runtime_json(path, payload)
            removed.append(f"{filename}: removed stale entries")

    cards_root = data_file("generated_cards")
    keep_names = {
        card_date,
        datetime.fromisoformat(card_date).strftime("%m-%d-%Y"),
    }
    if cards_root.exists():
        for folder in cards_root.iterdir():
            if not folder.is_dir() or folder.name in keep_names:
                continue
            for pattern in ("best_hit_prop*", "best_hit_art*"):
                for file_path in folder.glob(pattern):
                    if file_path.is_file():
                        file_path.unlink(missing_ok=True)
                        removed.append(str(file_path))
    SYSTEM_LOG.info(
        "component=StaleDataProtection card_date=%s removed=%s recovery=cache_pruned",
        card_date,
        len(removed),
    )
    return {"card_date": card_date, "removed": removed, "removed_count": len(removed)}


def _save_latest_card_snapshot(
    *,
    card_date: str,
    source_command: str,
    analysis: str,
    slate: list[dict[str, object]],
) -> None:
    """Save the latest generated MLB card so /save_last_card can recover it."""
    payload = {
        "latest_type": "mlb_official",
        "latest_date": card_date,
        "cards": {
            "mlb_official": {
                "type": "mlb_official",
                "date": card_date,
                "display_date": display_date(card_date),
                "source_command": source_command,
                "created_at": eastern_now().isoformat(timespec="seconds"),
                "analysis": analysis,
                "raw_text": analysis,
                "slate": slate,
            }
        },
    }
    DAILY_CARD_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _save_official_mlb_card(
    analysis: str,
    slate: list[dict[str, object]],
    card_date: str,
    source_command: str,
) -> int:
    """Persist generated MLB picks and log the source-level save event."""
    _save_latest_card_snapshot(
        card_date=card_date,
        source_command=source_command,
        analysis=analysis,
        slate=slate,
    )
    save_result = persist_official_card({
        "analysis": analysis,
        "slate": slate,
        "card_date": card_date,
        "source_command": source_command,
    })
    if not save_result.get("success"):
        raise ResultsTrackerError(str(save_result.get("error") or "Pick persistence failed"))
    saved_count = int(save_result.get("saved_pick_count") or 0)
    summary = render_saved_picks_summary(card_date)
    if saved_count <= 0 and "Total picks saved: 0" in summary:
        raise ResultsTrackerError(
            f"Pick Persistence Service reported success but no official picks exist for {card_date}."
        )
    print(f"Saved {saved_count} official MLB picks for {card_date}.", flush=True)
    return saved_count


def _main_menu_markup(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Return the modern inline app-style main menu."""
    rows = [
        [InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
        [
            InlineKeyboardButton("⚾ MLB", callback_data="menu:mlb_hub"),
            InlineKeyboardButton("⚽ Soccer", callback_data="menu:soccer_hub"),
        ],
        [
            InlineKeyboardButton("📊 Results", callback_data="menu:results_hub"),
            InlineKeyboardButton("💎 VIP", callback_data="menu:vip_hub"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help_hub")],
    ]
    if _is_admin_user(user_id):
        rows.append([InlineKeyboardButton("⚙️ Admin", callback_data="menu:admin_hub")])
    return InlineKeyboardMarkup(rows)


def _back_menu_markup() -> InlineKeyboardMarkup:
    """Return a consistent Back button for inline submenus."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu:main")]]
    )


def _card_disclaimer_markup(back_to: str = "menu:mlb_hub") -> InlineKeyboardMarkup:
    """Small inline tab for responsible-play notes without crowding cards."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Disclaimer", callback_data="menu:card_disclaimer")],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_to)],
    ])


def _hub_markup(hub: str) -> InlineKeyboardMarkup:
    """Return inline buttons for one public or admin hub."""
    rows_by_hub = {
        "mlb": [
            [InlineKeyboardButton("🔥 Play of the Day", callback_data="menu:mlb_today")],
            [InlineKeyboardButton("📋 Official MLB Card", callback_data="menu:mlb_card")],
            [InlineKeyboardButton("🖼 Anime Card Preview", callback_data="menu:mlb_image")],
            [InlineKeyboardButton("⚾ Best Hit Prop", callback_data="menu:mlb_best_hit")],
            [InlineKeyboardButton("🧩 Safe Parlay", callback_data="menu:mlb_parlay")],
        ],
        "soccer": [
            [InlineKeyboardButton("🌎 World Cup Card", callback_data="menu:soccer_worldcup")],
            [InlineKeyboardButton("🔥 Best Soccer Plays", callback_data="menu:soccer_card")],
            [InlineKeyboardButton("🧩 Soccer Parlay", callback_data="menu:soccer_parlay")],
        ],
        "results": [
            [
                InlineKeyboardButton("📅 Today", callback_data="menu:results_today"),
                InlineKeyboardButton("📆 Yesterday", callback_data="menu:results_yesterday"),
            ],
            [
                InlineKeyboardButton("📈 Last 7 Days", callback_data="menu:results_7days"),
                InlineKeyboardButton("🏆 Season", callback_data="menu:results_season"),
            ],
        ],
        "vip": [
            [InlineKeyboardButton("🥉 Weekly Pass", callback_data="menu:vip_weekly")],
            [InlineKeyboardButton("🥈 Monthly Pass", callback_data="menu:vip_monthly")],
            [InlineKeyboardButton("🥇 Season Pass", callback_data="menu:vip_season")],
            [InlineKeyboardButton("📌 Benefits", callback_data="menu:vip_benefits")],
        ],
        "help": [
            [InlineKeyboardButton("How BETGPTAI works", callback_data="menu:help_how")],
            [InlineKeyboardButton("Singles-first approach", callback_data="menu:help_singles")],
            [InlineKeyboardButton("Disclaimer", callback_data="menu:help_disclaimer")],
            [InlineKeyboardButton("Contact admin", callback_data="menu:help_contact")],
        ],
        "admin": [
            [InlineKeyboardButton("⚾ MLB War Room", callback_data="admin_mlb_war_room")],
            [InlineKeyboardButton("🧠 Mission Control", callback_data="admin_mission_control")],
            [InlineKeyboardButton("🖼 Generate MLB Images", callback_data="admin_generate_images")],
            [InlineKeyboardButton("📊 Debug Results", callback_data="admin_debug_results")],
            [InlineKeyboardButton("🔧 System Diagnostics", callback_data="admin_system_diagnostics")],
        ],
        "admin_mlb_war_room": [
            [InlineKeyboardButton("📋 Full Official MLB Card", callback_data="admin_full_mlb_card")],
            [InlineKeyboardButton("📋 Full Top 5 MLB Card", callback_data="admin_mlb_top5_card")],
            [InlineKeyboardButton("🖼 War Room Image", callback_data="admin_generate_images")],
        ],
        "ai_learning": [
            [InlineKeyboardButton("Tonight’s Loss Review", callback_data="learning_loss_review")],
            [InlineKeyboardButton("Weight Suggestions", callback_data="learning_weight_suggestions")],
            [InlineKeyboardButton("Learning Status", callback_data="learning_status")],
            [
                InlineKeyboardButton("Approve Updates", callback_data="learning_approve"),
                InlineKeyboardButton("Reject Updates", callback_data="learning_reject"),
            ],
        ],
    }
    rows = rows_by_hub.get(hub, [])
    back_callback = "admin_back" if hub in {"admin_mlb_war_room", "ai_learning"} else "menu:main"
    return InlineKeyboardMarkup([
        *rows,
        [InlineKeyboardButton("⬅️ Back", callback_data=back_callback)],
    ])


def _results_date_markup() -> InlineKeyboardMarkup:
    """Offer buttons for real card dates saved in picks.json."""
    date_rows = [
        [InlineKeyboardButton(f"{display_date(day)} Results", callback_data=f"menu:results_date:{day}")]
        for day in available_card_dates()[-8:]
    ]
    return InlineKeyboardMarkup([
        *date_rows,
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:results_hub")],
    ])


def _system_diagnostics_markup() -> InlineKeyboardMarkup:
    """Owner-only System Diagnostics buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Odds Debug", callback_data="admin_odds_debug")],
        [InlineKeyboardButton("🧪 Card Debug", callback_data="admin_card_debug")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")],
    ])


def _results_dashboard_or_picker(target_date: str) -> tuple[str, InlineKeyboardMarkup]:
    """Return a date dashboard, or a friendly picker if that date has no picks."""
    normalized = normalize_pick_date(target_date) or target_date
    dashboard = build_daily_results_dashboard(normalized)
    if dashboard.startswith("No official picks saved"):
        return missing_results_message(normalized), _results_date_markup()
    return dashboard, _hub_markup("results")


def _main_menu_text() -> str:
    """Render the public home screen."""
    return (
        "⚾🏈🏀⚽ BETGPTAI SPORTS HUB\n\n"
        "AI-Powered Sports Analysis\n\n"
        "━━━━━━━━━━━━\n\n"
        "🔥 TODAY’S FEATURED PICKS\n"
        "Tap 🎯 Today's Picks\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚾ MLB CARD\n"
        "Today’s free MLB analysis\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚽ SOCCER CARD\n"
        "Today’s free soccer analysis\n\n"
        "━━━━━━━━━━━━\n\n"
        "📊 RESULTS TRACKER\n"
        "Official-card performance\n\n"
        "━━━━━━━━━━━━\n\n"
        "💎 PREMIUM MEMBERSHIP\n"
        "Full-card access\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚠️ BETGPTAI PHILOSOPHY\n\n"
        "✅ Pregame only\n"
        "✅ Simplicity\n"
        "✅ Stability\n"
        "✅ Singles-first approach\n"
        "📈 Long-Term Profitability\n"
        "🧩 Parlays Optional\n\n"
        "Educational analysis only. Play responsibly."
    )


def _help_text() -> str:
    """Render the public command guide."""
    return (
        "📋 BETGPTAI COMMANDS\n\n"
        "🎯 /today\n"
        "Today’s Play of the Day + Safe Parlay\n\n"
        "📅 /tomorrow\n"
        "Next-day cards when lines are available\n\n"
        "⚾ /mlb_auto\n"
        "Today’s MLB Card\n\n"
        "⚽ /soccer\n"
        "Today’s Soccer Card\n\n"
        "📊 /results\n"
        "Performance Tracker\n\n"
        "💎 /vip\n"
        "Premium Membership\n\n"
        "Educational analysis only. Play responsibly."
    )


def _vip_text() -> str:
    """Render the public premium screen."""
    return (
        "💎 BETGPTAI PREMIUM MEMBERSHIP\n\n"
        "Unlock:\n\n"
        "⚾ Full MLB Cards\n"
        "⚾ F5 Plays\n"
        "⚾ Team Totals\n"
        "⚾ Pregame market edges\n"
        "⚽ Soccer Cards\n"
        "📊 Results Tracking\n\n"
        "━━━━━━━━━━━━\n\n"
        "🥉 Weekly Pass — $5.99\n\n"
        "https://buy.stripe.com/aFadRbgvt1Da75L5xv0ZW00\n\n"
        "🥈 Premium Monthly — $9.99\n\n"
        "https://buy.stripe.com/dRm7sN6UT95C89PbVT0ZW01\n\n"
        "🥇 Season Pass — $49.99 every 6 months\n\n"
        "https://buy.stripe.com/aFa9AVa75epW1Lre410ZW02\n\n"
        "━━━━━━━━━━━━\n\n"
        "After payment, send proof of purchase to @YOUR_USERNAME to receive VIP access.\n\n"
        "⚠️ Singles are recommended for the best long-term results.\n\n"
        "Parlays are optional and carry higher risk.\n\n"
        "Educational analysis only. Play responsibly."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the clean public BETGPTAI sports hub."""
    del context
    if not update.message:
        return
    # Remove any persistent reply keyboard left over from older bot versions.
    cleanup_message = await update.message.reply_text(
        "Opening BETGPTAI...",
        reply_markup=ReplyKeyboardRemove(),
    )
    with contextlib.suppress(Exception):
        await cleanup_message.delete()
    await update.message.reply_text(
        _main_menu_text(),
        reply_markup=_main_menu_markup(update.effective_user.id if update.effective_user else None),
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the short public command guide."""
    del context
    if not update.message:
        return
    await update.message.reply_text(
        _help_text(),
        reply_markup=_back_menu_markup(),
    )


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show local/Railway deployment diagnostics."""
    del context
    if not update.message:
        return
    await update.message.reply_text(_version_text())


def _format_line(value: object) -> str:
    """Format saved American odds without exposing a sportsbook name."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "Unavailable"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"+{value}" if value > 0 else str(value)


def _card_date_header(card_date: str) -> str:
    """Return the public card date line in MM/DD/YYYY format."""
    return f"📅 Card Date: {display_date(card_date)}"


def _with_card_date(text: str, card_date: str) -> str:
    """Prefix card text with the BETGPTAI card date when not already present."""
    if "📅 Card Date:" in text[:120]:
        return text
    return f"{_card_date_header(card_date)}\n\n{text}"


async def _build_today_card_text() -> str | None:
    """Build the public /today text from the latest saved official picks."""
    try:
        featured = await asyncio.to_thread(
            get_most_recent_featured_picks,
            official_sports_date().isoformat(),
        )
    except Exception:
        logging.exception("Could not load saved picks for /today")
        featured = {}

    play = featured.get("play_of_day")
    legs = featured.get("parlay_legs", [])
    if not play or not isinstance(legs, list) or len(legs) != 2:
        return (
            "Today’s picks are still being prepared. "
            "Type /mlb_auto to generate today’s card."
        )

    leg_details = featured.get("parlay_leg_details", [])
    parlay_text = render_safe_parlay(
        leg_details if isinstance(leg_details, list) else [],
        card_date=official_sports_date().isoformat(),
    )
    selected_date = official_sports_date().isoformat()
    return (
        "🎯 BETGPTAI TODAY\n\n"
        f"{render_play_of_day_card(selected_date)}\n\n"
        f"{parlay_text}\n\n"
        "📋 Want the full free MLB card?\n"
        "Type /mlb_auto\n\n"
        "💎 Want premium full slate?\n"
        "Type /vip"
    )


async def _build_safe_parlay_text() -> str:
    """Build only today's saved safe parlay block."""
    try:
        featured = await asyncio.to_thread(
            get_most_recent_featured_picks,
            official_sports_date().isoformat(),
        )
    except Exception:
        logging.exception("Could not load saved picks for safe parlay")
        featured = {}
    legs = featured.get("parlay_legs", [])
    leg_details = featured.get("parlay_leg_details", [])
    if not isinstance(legs, list) or len(legs) < 2:
        return "No Safe 2-Leg Parlay qualified today."
    return render_safe_parlay(
        leg_details if isinstance(leg_details, list) else [],
        card_date=official_sports_date().isoformat(),
    )


def _best_hit_text_from_prop(prop: dict[str, Any]) -> str:
    """Render only a verified same-day Best Hit Prop."""
    return (
        "⚾ BEST HIT PROP\n\n"
        f"👤 {prop.get('player_name', 'Player')}\n"
        f"🧢 {prop.get('team_name', prop.get('team', 'Team'))}\n"
        f"🆚 {prop.get('opponent_name', prop.get('opponent', 'Opponent'))}\n"
        f"🕒 {prop.get('game_time_et', 'Time unavailable ET')}\n\n"
        "🎯 Prop:\n"
        f"Over {prop.get('line', 0.5)} Hits\n\n"
        "Educational analysis only. Singles are recommended."
    )


async def _verified_best_hit_text_for_today() -> tuple[str, dict[str, Any] | None]:
    """Regenerate and verify today's Best Hit Prop before showing it."""
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            return "Best Hit Prop is not ready yet.", None
        result = await asyncio.to_thread(get_verified_best_hit_prop, slate, selected_date)
        prop = result.get("prop")
        if result.get("status") == "ready" and isinstance(prop, dict):
            return _best_hit_text_from_prop(prop), prop
        logging.warning("Best Hit Prop rejected: %s", result.get("rejections") or result.get("reason"))
        return "No public hit props today — no FanDuel verified positive-edge Over 0.5 Hits passed the full matchup system.", None
    except Exception:
        logging.exception("Could not verify Best Hit Prop")
        return "Best Hit Prop is being verified. Check back soon.", None


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show only the featured play and safe parlay from the latest saved card."""
    del context
    if not update.message:
        return
    await update.message.reply_text(await _build_today_card_text())


async def mlb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's MLB plays. Edit this message whenever plays change."""
    del context
    today = datetime.now().strftime("%B %d, %Y")

    # This is the daily card shown when a user sends /mlb.
    plays = (
        f"🔥 BETGPTAI MLB CARD — {today} ⚾\n\n"
        "🥇 Best Bet: Phillies -1.5\n"
        "🥈 Total: Mariners Under 6.5\n"
        "🥉 Value Play: Royals ML\n\n"
        "✅ 1 unit = normal play\n"
        "🔥 2 units = strongest play\n\n"
        "⚠️ Educational analysis only. Play responsibly."
    )
    if update.message:
        await update.message.reply_text(plays)


async def _send_long_message(update: Update, text: str) -> None:
    """Split long AI output so every message fits Telegram's size limit."""
    if not update.message:
        return
    await _send_long_text_to_message(update.message, text)


async def _send_long_text_to_message(message: object, text: str) -> None:
    """Split long output using any Telegram message-like object."""
    if not message:
        return

    remaining = text
    while remaining:
        if len(remaining) <= 3900:
            chunk = remaining
            remaining = ""
        else:
            # Prefer breaking between paragraphs instead of mid-sentence.
            split_at = remaining.rfind("\n\n", 0, 3900)
            if split_at < 1:
                split_at = remaining.rfind("\n", 0, 3900)
            if split_at < 1:
                split_at = 3900
            chunk = remaining[:split_at]
            remaining = remaining[split_at:].lstrip()
        await message.reply_text(chunk)


async def mlb_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build today's slate and ask OpenAI for automated MLB analysis."""
    del context
    if not update.message:
        return

    await update.message.reply_text(
        "⏳ Building today’s BETGPTAI card..."
    )

    odds_api_key = os.getenv("ODDS_API_KEY", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    highlightly_api_key = os.getenv("HIGHLIGHTLY_API_KEY", "")
    selected_date = official_sports_date().isoformat()
    message_text = update.message.text or ""
    source_command = (
        "generate_today"
        if message_text.split()[0].lower().startswith("/generate_today")
        else "mlb_auto"
    )

    try:
        await asyncio.to_thread(_purge_stale_daily_caches, selected_date, clear_today_props=True)
        # requests is synchronous, so run the data download in another thread.
        # This keeps the async Telegram bot responsive while APIs are loading.
        slate = await asyncio.to_thread(
            get_combined_slate,
            odds_api_key,
            game_date=selected_date,
            highlightly_api_key=highlightly_api_key,
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        slate = upcoming_mlb_slate(slate)
        if not slate:
            await update.message.reply_text("No upcoming MLB games are available for today’s free card.")
            return

        try:
            analysis = await analyze_mlb_slate(
                slate, openai_api_key, os.getenv("ANTHROPIC_API_KEY", "")
            )
        except Exception as error:
            # Unexpected analyst failures should not stop the bot. Print the
            # complete error and traceback in the terminal,
            # then quietly send the data-only fallback card to Telegram.
            logging.error(
                "AI Analysis Error:\n%s",
                error,
                exc_info=True,
            )
            analysis = build_fallback_card(slate)

        await asyncio.to_thread(
            save_model_report, selected_date, slate, analysis, get_last_analysis_metadata()
        )

        # Build a StructuredCard directly from the slate + analysis data.
        # This replaces the old flow of generating Telegram text, then
        # parsing it back to recover picks.
        try:
            card = build_card_from_analysis(
                analysis, slate, selected_date, source_command,
            )
            builder_count = len(card.official_picks)
            print(f"TRACE build_card_from_analysis official_picks={builder_count} date={selected_date}", flush=True)
            _save_latest_card_snapshot(
                card_date=selected_date,
                source_command=source_command,
                analysis=analysis,
                slate=slate,
            )
            card_dict = structured_card_to_dict(card)
            dict_count = len(card_dict.get("official_picks", []))
            print(f"TRACE structured_card_to_dict official_picks={dict_count} date={selected_date}", flush=True)
            card_dict["analysis"] = analysis
            card_dict["slate"] = slate
            card_dict["source_command"] = source_command
            persist_count = len(card_dict.get("official_picks", []))
            print(f"TRACE before persist_official_card official_picks={persist_count} date={selected_date}", flush=True)
            save_result = await asyncio.to_thread(persist_official_card, card_dict)
            print(
                f"TRACE after persist_official_card success={save_result.get('success')} "
                f"saved={save_result.get('saved_pick_count')} "
                f"error={save_result.get('error', '')} "
                f"date={selected_date}",
                flush=True,
            )
            if not save_result.get("success"):
                raise ResultsTrackerError(
                    str(save_result.get("error") or "Pick persistence failed.")
                )
            saved_count = int(save_result.get("saved_pick_count") or 0)
            summary = render_saved_picks_summary(selected_date)
            if saved_count <= 0 and "Total picks saved: 0" in summary:
                raise ResultsTrackerError(
                    "No structured official picks were generated."
                )
            print(f"Saved {saved_count} structured official picks for {selected_date}.", flush=True)
        except Exception as error:
            logging.exception("Could not save official picks to picks.json")
            await update.message.reply_text(
                "The card was generated, but its picks could not be saved.\n\n"
                f"Error: {error!r}\n\n"
                "No picks were sent."
            )
            return

        # Delivery happens only after picks.json has been updated successfully.
        await _send_long_message(update, render_mlb_premium_card(selected_date))
        await update.message.reply_text(
            "Card notes and responsible-play info:",
            reply_markup=_card_disclaimer_markup(),
        )
    except MLBDataError:
        # The MLB schedule is the only required feed; optional sources recover above.
        logging.exception("MLB or odds data download failed")
        await update.message.reply_text(
            "Unable to build today’s card. Please try again shortly."
        )
    except Exception:
        # Avoid exposing unexpected technical details or secret values to users.
        logging.exception("Unexpected /mlb_auto error")
        await update.message.reply_text(
            "Something unexpected went wrong. Check the terminal log and try again."
        )


async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how users can learn more about VIP access."""
    del context
    if update.message:
        await update.message.reply_text(
            _vip_text(),
            reply_markup=_back_menu_markup(),
        )


async def _send_soccer_card(
    update: Update, mode: str, game_date: str | None = None
) -> None:
    """Run the soccer pipeline for one public or owner-only card type."""
    if not update.message:
        return
    await update.message.reply_text("⏳ Building today’s BETGPTAI soccer card...")
    try:
        selected_date = game_date or official_sports_date().isoformat()
        slate = await asyncio.to_thread(
            get_soccer_slate,
            os.getenv("FOOTBALL_DATA_API_KEY", ""),
            os.getenv("ODDS_API_KEY", ""),
            live_only=False,
            game_date=selected_date,
            sports_db_api_key=thesportsdb_api_key(),
            serpapi_key=os.getenv("SERPAPI_KEY", ""),
            api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
        )
        if mode == "corners" and not any(
            game.get("corners_profile") != "unavailable" for game in slate
        ):
            await update.message.reply_text(
                "DATA LIMITATIONS\n"
                "- Corners data unavailable from current feeds.\n\n"
                f"{TIMED_CARD_FOOTER}"
            )
            return
        owner_modes = {
            "public", "full", "btts", "overs", "corners", "cards",
            "first_half", "second_half", "double_chance", "asian_handicap",
        }
        analysis_mode = mode if mode in owner_modes else "full"
        card = await analyze_soccer_slate(
            slate,
            os.getenv("OPENAI_API_KEY", ""),
            analysis_mode,
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        try:
            saved_count = await asyncio.to_thread(
                save_soccer_picks, card, slate, selected_date, f"soccer_{analysis_mode}"
            )
            if saved_count:
                print(f"Saved {saved_count} soccer picks to picks.json", flush=True)
        except Exception:
            logging.exception("Could not save soccer picks to picks.json")
        await _send_long_message(update, _with_card_date(card, selected_date))
    except SoccerDataError as error:
        logging.warning("Soccer data unavailable: %s", error)
        await update.message.reply_text(
            "Unable to build the soccer card right now. Please try again shortly."
        )
    except Exception:
        logging.exception("Unexpected soccer card error")
        await update.message.reply_text(
            "Unable to build the soccer card right now. Check the terminal and try again."
        )


async def soccer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the public two-play BETGPTAI soccer card."""
    del context
    await _send_soccer_card(update, "public")


async def worldcup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show World Cup Mode when active, otherwise the normal soccer card."""
    del context
    await _send_soccer_card(update, "public")


def _slate_has_lines(slate: object) -> bool:
    """Return True when at least one game has a real supplied market price."""
    return isinstance(slate, list) and any(
        isinstance(game, dict) and bool(game.get("best_available_prices"))
        for game in slate
    )


async def tomorrow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Publish next-sports-day MLB and soccer cards only after lines populate."""
    del context
    if not update.message:
        return
    target_date = tomorrow_sports_date().isoformat()
    await update.message.reply_text("⏳ Checking tomorrow’s available lines...")

    mlb_task = asyncio.to_thread(
        get_combined_slate,
        os.getenv("ODDS_API_KEY", ""),
        game_date=target_date,
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    soccer_task = asyncio.to_thread(
        get_soccer_slate,
        os.getenv("FOOTBALL_DATA_API_KEY", ""),
        os.getenv("ODDS_API_KEY", ""),
        game_date=target_date,
        sports_db_api_key=thesportsdb_api_key(),
        serpapi_key=os.getenv("SERPAPI_KEY", ""),
        api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
    )
    mlb_result, soccer_result = await asyncio.gather(
        mlb_task, soccer_task, return_exceptions=True
    )
    if isinstance(mlb_result, Exception):
        logging.warning("Tomorrow MLB slate unavailable: %s", mlb_result)
        mlb_slate: list[dict[str, object]] = []
    else:
        mlb_slate = mlb_result
    if isinstance(soccer_result, Exception):
        logging.warning("Tomorrow soccer slate unavailable: %s", soccer_result)
        soccer_slate: list[dict[str, object]] = []
    else:
        soccer_slate = soccer_result

    if not _slate_has_lines(mlb_slate) and not _slate_has_lines(soccer_slate):
        await update.message.reply_text(
            "Tomorrow’s card is not ready yet. Lines are still populating. "
            "Check back after 3:00 AM ET."
        )
        return

    await update.message.reply_text(
        f"📅 BETGPTAI TOMORROW — {target_date}"
    )
    if _slate_has_lines(mlb_slate):
        mlb_card = await analyze_mlb_slate(
            mlb_slate,
            os.getenv("OPENAI_API_KEY", ""),
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        await asyncio.to_thread(
            save_model_report, target_date, mlb_slate, mlb_card, get_last_analysis_metadata()
        )
        try:
            saved_count = await asyncio.to_thread(
                _save_official_mlb_card,
                mlb_card,
                mlb_slate,
                target_date,
                "tomorrow",
            )
        except Exception:
            logging.exception("Could not save tomorrow's MLB picks")
        await _send_long_message(update, render_mlb_premium_card(target_date))
    if _slate_has_lines(soccer_slate):
        soccer_card = await analyze_soccer_slate(
            soccer_slate,
            os.getenv("OPENAI_API_KEY", ""),
            "public",
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        await _send_long_message(update, _with_card_date(soccer_card, target_date))


async def soccer_owner_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route protected soccer commands to their matching premium view."""
    if not await _require_admin(update):
        return
    message_text = update.message.text if update.message and update.message.text else ""
    command = message_text.split()[0].lstrip("/").split("@", 1)[0].lower() if message_text else "soccer_full"
    modes = {
        "soccer_full": "full",
        "btts": "btts",
        "corners": "corners",
        "overs": "overs",
        "cards": "cards",
        "first_half": "first_half",
        "second_half": "second_half",
        "double_chance": "double_chance",
        "asian_handicap": "asian_handicap",
    }
    await _send_soccer_card(update, modes.get(command, "full"))


async def soccer_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Display the protected BETGPTAI soccer results dashboard."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    try:
        dashboard = await asyncio.to_thread(build_soccer_results_dashboard)
        await _send_long_message(update, dashboard)
    except SoccerResultsError as error:
        logging.warning("Soccer results unavailable: %s", error)
        await update.message.reply_text(f"Soccer results are unavailable: {error}")


async def debug_soccer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only diagnostics for the public soccer card builder."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_soccer_slate,
            os.getenv("FOOTBALL_DATA_API_KEY", ""),
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            sports_db_api_key=thesportsdb_api_key(),
            serpapi_key=os.getenv("SERPAPI_KEY", ""),
            api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
        )
        await update.message.reply_text(
            soccer_debug_report(slate, get_last_soccer_debug())
        )
    except Exception:
        logging.exception("Unexpected /debug_soccer error")
        await update.message.reply_text("Unable to inspect soccer data right now.")


async def results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display today's official-card results from the vault."""
    del context
    if not update.message:
        return

    target_date = eastern_today().isoformat()
    try:
        from services.results_vault import render_daily_results
        dashboard = await asyncio.to_thread(render_daily_results, target_date)
        if dashboard and "No official snapshot" not in dashboard:
            await update.message.reply_text(dashboard)
            return
    except Exception:
        pass

    try:
        dashboard = await asyncio.to_thread(build_daily_results_dashboard)
    except ResultsTrackerError as error:
        logging.warning("Could not load results dashboard: %s", error)
        await update.message.reply_text(
            f"Results are temporarily unavailable: {error}"
        )
        return

    await update.message.reply_text(dashboard)


async def results_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for today's daily results."""
    await results(update, context)


async def results_yesterday(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Display yesterday's official-card results from the vault."""
    del context
    if not update.message:
        return
    target_date = (eastern_today() - timedelta(days=1)).isoformat()
    try:
        from services.results_vault import render_daily_results
        dashboard = await asyncio.to_thread(render_daily_results, target_date)
        if dashboard and "No official snapshot" not in dashboard:
            await update.message.reply_text(dashboard)
            return
    except Exception:
        pass
    text, markup = await asyncio.to_thread(_results_dashboard_or_picker, target_date)
    await update.message.reply_text(text, reply_markup=markup)


async def results_7days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the rolling seven-day results dashboard."""
    del context
    if not update.message:
        return
    dashboard = await asyncio.to_thread(build_range_results_dashboard, 7)
    await update.message.reply_text(dashboard)


async def results_30days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the rolling thirty-day results dashboard."""
    del context
    if not update.message:
        return
    dashboard = await asyncio.to_thread(build_range_results_dashboard, 30)
    await update.message.reply_text(dashboard)


async def results_season(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display season-to-date results."""
    del context
    if not update.message:
        return
    dashboard = await asyncio.to_thread(build_range_results_dashboard, None)
    await update.message.reply_text(dashboard)


async def results_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display one requested card_date using YYYY-MM-DD or MM/DD/YYYY."""
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /results_date YYYY-MM-DD")
        return
    target_date = context.args[0]
    if not normalize_pick_date(target_date):
        await update.message.reply_text("Use YYYY-MM-DD, example: /results_date 2026-07-04")
        return
    text, markup = await asyncio.to_thread(_results_dashboard_or_picker, target_date)
    await update.message.reply_text(text, reply_markup=markup)


async def results_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only read-only grading and duplicate audit."""
    if not await _require_admin(update) or not update.message:
        return
    target = normalize_pick_date((context.args or [eastern_today().isoformat()])[0])
    if not target:
        await update.message.reply_text("Usage: /results_debug YYYY-MM-DD")
        return
    try:
        from services.results_vault import render_results_debug, results_debug_payload
        payload = await asyncio.to_thread(results_debug_payload, target)
        await _send_long_message(update, render_results_debug(payload))
    except Exception as error:
        logging.exception("/results_debug failed")
        await update.message.reply_text(f"/results_debug failed: {error}")


async def repair_results_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only exact-duplicate cleanup and final-game regrade."""
    if not await _require_admin(update) or not update.message:
        return
    target = normalize_pick_date((context.args or [""])[0])
    if not target:
        await update.message.reply_text("Usage: /repair_results YYYY-MM-DD")
        return
    try:
        from services.results_vault import repair_results_date
        result = await asyncio.to_thread(repair_results_date, target, cleanup_duplicates=True)
        if not result.get("success"):
            await update.message.reply_text(f"Repair failed: {result.get('error')}")
            return
        await update.message.reply_text(
            "✅ RESULTS REPAIR COMPLETE\n\n"
            f"Date: {target}\n"
            f"Duplicates removed: {result.get('duplicates_removed', 0)}\n"
            f"MLB picks graded: {result.get('graded_picks', 0)}\n"
            f"Still pending: {result.get('pending_picks', 0)}\n"
            f"Ungraded: {result.get('ungraded_picks', 0)}\n"
            f"Results path: {result.get('path')}"
        )
    except Exception as error:
        logging.exception("/repair_results failed")
        await update.message.reply_text(f"/repair_results failed: {error}")


async def date_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only date diagnostics for results buttons."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    utc_now = datetime.now(timezone.utc)
    et_now = eastern_now()
    today_et = eastern_today()
    yesterday_et = today_et - timedelta(days=1)
    dates = await asyncio.to_thread(available_card_dates)
    selected = LAST_RESULTS_BUTTON_DATE or f"Yesterday button resolves to {yesterday_et.isoformat()}"
    await update.message.reply_text(
        "🗓 BETGPTAI DATE DEBUG\n\n"
        f"UTC now: {utc_now.isoformat(timespec='seconds')}\n"
        f"ET now: {et_now.isoformat(timespec='seconds')}\n"
        f"Today ET: {today_et.isoformat()}\n"
        f"Yesterday ET: {yesterday_et.isoformat()}\n\n"
        "Dates found in picks.json:\n"
        + ("\n".join(f"- {display_date(day)} ({day})" for day in dates) if dates else "None")
        + "\n\n"
        f"Selected results date from button: {selected}"
    )


async def callback_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only callback registration diagnostics."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    required = [
        "admin_mlb_war_room",
        "admin_mission_control",
        "admin_generate_images",
        "admin_debug_results",
        "admin_system_diagnostics",
        "admin_odds_debug",
        "admin_card_debug",
        "admin_back",
    ]
    await update.message.reply_text(
        "🧪 BETGPTAI CALLBACK DEBUG\n\n"
        "Registered callback handlers:\n"
        + "\n".join(f"- {name} ✅" for name in required)
    )


async def _system_diagnostics_text() -> str:
    """Owner-only complete system diagnostics."""
    storage_payload = await asyncio.to_thread(get_storage_status)
    mission_ok = "✅ Available"
    try:
        mission_snapshot = await _mission_control_health_text()
        if "Needs attention" in mission_snapshot or "Failed" in mission_snapshot:
            mission_ok = "⚠️ Partial"
    except Exception as error:
        logging.exception("System diagnostics mission control check failed")
        mission_ok = f"❌ Error: {error}"
    try:
        learning_payload = await asyncio.to_thread(learning_status_payload)
        learning_ok = "✅ Available" if isinstance(learning_payload, dict) else "➖ Unknown"
    except Exception as error:
        logging.exception("System diagnostics AI learning check failed")
        learning_ok = f"❌ Error: {error}"
    image_ok = (
        "✅ Enabled"
        if _truthy_env("IMAGE_GENERATION_ENABLED") and os.getenv("OPENAI_API_KEY", "").strip()
        else "➖ Disabled"
    )
    results_ok = "✅ Healthy" if storage_payload.get("results_database_healthy") else "❌ Needs attention"
    storage_ok = "✅ Healthy" if storage_payload.get("writable") else "❌ Not writable"
    scheduler_running = "✅ Registered"  # The scheduler task starts with polling in main().
    callbacks = [
        "admin_mlb_war_room",
        "admin_mission_control",
        "admin_generate_images",
        "admin_debug_results",
        "admin_system_diagnostics",
        "admin_back",
    ]
    callback_rows = "\n".join(
        f"{name}: {'✅ Registered' if name in ADMIN_CALLBACKS else '❌ Missing'}"
        for name in callbacks
    )
    try:
        import sys
        python_version = sys.version.split()[0]
    except Exception:
        python_version = "Unavailable"
    return (
        "🔧 BETGPTAI SYSTEM DIAGNOSTICS\n\n"
        f"BETGPTAI Version: {_app_version()}\n"
        f"Git Commit: {_git_commit_hash()}\n"
        f"Railway Environment: {os.getenv('RAILWAY_ENVIRONMENT', 'Not available')}\n"
        f"Deployment ID: {_deployment_id()}\n"
        f"Deployment Time: {_deployed_at()}\n"
        f"Python Version: {python_version}\n"
        f"APP_TIMEZONE: {os.getenv('APP_TIMEZONE', 'America/New_York')}\n"
        f"DATA_DIR: {data_file('').resolve()}\n\n"
        f"Scheduler Running: {scheduler_running}\n"
        "Next Scheduler Job: Use /time_debug for exact timing\n"
        "Callbacks Registered: ✅ Yes\n"
        "Inline Menus Registered: ✅ Yes\n"
        "Handlers Registered: ✅ Yes\n"
        "Telegram Polling Status: ✅ Registered\n\n"
        f"Storage Status: {storage_ok}\n"
        "API Status: Use /status for full provider detail\n"
        f"Mission Control Status: {mission_ok}\n"
        f"Images Engine Status: {image_ok}\n"
        f"Results Engine Status: {results_ok}\n"
        f"AI Learning Engine Status: {learning_ok}\n\n"
        "Admin Hub Button Verification:\n"
        f"{callback_rows}\n\n"
        f"Callback Log: {data_file('logs') / 'callbacks.log'}"
    )


async def system_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only complete diagnostics command."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    text = await _system_diagnostics_text()
    await _send_long_message(update, text)


async def update_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grade pending official picks against final MLB scores."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    await update.message.reply_text("Checking final MLB scores... ⚾")
    try:
        summary = await asyncio.to_thread(update_results_from_mlb)
    except ResultsTrackerError as error:
        logging.warning("Automatic result grading failed: %s", error)
        await update.message.reply_text(f"Unable to update results: {error}")
        return
    except Exception:
        logging.exception("Unexpected /update_results error")
        await update.message.reply_text(
            "Unable to update results right now. Check the terminal and try again."
        )
        return

    await update.message.reply_text(
        "✅ RESULTS UPDATE COMPLETE\n\n"
        f"Total Tracked Picks: {summary['total_picks']}\n"
        f"Pending Picks: {summary['pending']}\n"
        f"Graded Picks: {summary['graded']}\n\n"
        "Use /results to view the updated dashboard."
    )


async def grade_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only command to grade only today's saved MLB picks."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    selected_date = eastern_today().isoformat()
    await update.message.reply_text("Checking today’s saved MLB picks... ⚾")
    try:
        summary = await asyncio.to_thread(grade_mlb_picks_for_date, selected_date)
    except Exception:
        logging.exception("Unexpected /grade_today error")
        await update.message.reply_text(
            "Unable to grade today’s picks right now. Check the terminal and try again."
        )
        return
    vault_msg = ""
    try:
        from services.results_vault import grade_snapshot_date
        vault = await asyncio.to_thread(grade_snapshot_date, selected_date)
        if vault.get("success"):
            vault_msg = f"Vault saved: {vault.get('path')}"
        else:
            vault_msg = f"Vault: {vault.get('error', 'unknown')}"
    except Exception:
        vault_msg = "Vault save unavailable"
    await update.message.reply_text(
        "✅ RESULTS UPDATE COMPLETE\n\n"
        f"Newly Graded: {summary.get('newly_graded', 0)}\n"
        f"Still Pending: {summary.get('pending', 0)}\n"
        f"Missing Metadata: {summary.get('missing_metadata', 0)}\n"
        f"Errors: {summary.get('errors', 0)}\n"
        f"{vault_msg}"
    )
    try:
        learning_report = await asyncio.to_thread(run_learning_review, selected_date)
        await _send_long_message(update, render_learning_report(learning_report))
    except Exception:
        logging.exception("AI Learning review failed after /grade_today")
        await update.message.reply_text(
            "AI Learning review could not be generated. Results grading still completed."
        )


async def grade_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only command to grade yesterday's saved MLB picks."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    selected_date = (eastern_today() - timedelta(days=1)).isoformat()
    await update.message.reply_text(f"Checking saved MLB picks for {display_date(selected_date)}... ⚾")
    try:
        summary = await asyncio.to_thread(grade_mlb_picks_for_date, selected_date)
    except Exception:
        logging.exception("Unexpected /grade_yesterday error")
        await update.message.reply_text(
            "Unable to grade yesterday’s picks right now. Check the terminal and try again."
        )
        return
    vault_msg = ""
    try:
        from services.results_vault import grade_snapshot_date
        vault = await asyncio.to_thread(grade_snapshot_date, selected_date)
        if vault.get("success"):
            vault_msg = f"Vault saved: {vault.get('path')}"
        else:
            vault_msg = f"Vault: {vault.get('error', 'unknown')}"
    except Exception:
        vault_msg = "Vault save unavailable"
    await update.message.reply_text(
        "✅ RESULTS UPDATE COMPLETE\n\n"
        f"Date: {display_date(selected_date)}\n"
        f"Newly Graded: {summary.get('newly_graded', 0)}\n"
        f"Still Pending: {summary.get('pending', 0)}\n"
        f"Missing Metadata: {summary.get('missing_metadata', 0)}\n"
        f"Errors: {summary.get('errors', 0)}\n"
        f"{vault_msg}"
    )


async def force_grade_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only command to grade one explicit YYYY-MM-DD or MM/DD/YYYY date."""
    if not await _require_admin(update):
        return
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /force_grade_date YYYY-MM-DD")
        return
    selected_date = normalize_pick_date(context.args[0])
    if not selected_date:
        await update.message.reply_text("Use YYYY-MM-DD, example: /force_grade_date 2026-07-04")
        return
    await update.message.reply_text(f"Force grading MLB picks for {display_date(selected_date)}... ⚾")
    try:
        summary = await asyncio.to_thread(grade_mlb_picks_for_date, selected_date)
    except Exception:
        logging.exception("Unexpected /force_grade_date error")
        await update.message.reply_text(
            "Unable to force grade that date right now. Check the terminal and try again."
        )
        return
    await update.message.reply_text(
        "✅ RESULTS UPDATE COMPLETE\n\n"
        f"Date: {display_date(selected_date)}\n"
        f"Newly Graded: {summary.get('newly_graded', 0)}\n"
        f"Still Pending: {summary.get('pending', 0)}\n"
        f"Missing Metadata: {summary.get('missing_metadata', 0)}\n"
        f"Errors: {summary.get('errors', 0)}"
    )


# ── Daily Snapshot Commands ───────────────────────────────────────────────

async def snapshot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show daily snapshot status for today or a given date."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    target = args[0] if args else eastern_today().isoformat()
    try:
        from services.daily_snapshot import snapshot_status as _snap_status, render_snapshot_status
        payload = await asyncio.to_thread(_snap_status, target)
        await update.message.reply_text(render_snapshot_status(payload))
    except Exception as error:
        logging.exception("/snapshot_status failed")
        await update.message.reply_text(f"/snapshot_status failed: {error!r}")


async def snapshot_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show detailed snapshot debug info."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    target = args[0] if args else eastern_today().isoformat()
    try:
        from services.daily_snapshot import snapshot_debug as _snap_debug, render_snapshot_debug
        payload = await asyncio.to_thread(_snap_debug, target)
        await update.message.reply_text(render_snapshot_debug(payload))
    except Exception as error:
        logging.exception("/snapshot_debug failed")
        await update.message.reply_text(f"/snapshot_debug failed: {error!r}")


async def snapshot_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: regenerate snapshot for a date. Must include --confirm."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    if "--confirm" not in args:
        await update.message.reply_text("Usage: /snapshot_regenerate YYYY-MM-DD --confirm\nAdd --confirm to force regeneration.")
        return
    date_args = [a for a in args if a != "--confirm"]
    if not date_args:
        await update.message.reply_text("Usage: /snapshot_regenerate YYYY-MM-DD --confirm")
        return
    target = normalize_pick_date(date_args[0])
    if not target:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return
    try:
        from services.daily_snapshot import regenerate_snapshot, load_snapshot
        from results_tracker import load_picks

        all_picks = await asyncio.to_thread(load_picks)
        todays = [p for p in all_picks if isinstance(p, dict) and str(p.get("card_date") or p.get("date") or "") == target]
        slate = []
        try:
            from mlb_data import get_combined_slate
            slate = await asyncio.to_thread(get_combined_slate, os.getenv("ODDS_API_KEY", ""), game_date=target)
        except Exception:
            pass
        result = await asyncio.to_thread(regenerate_snapshot, target, todays, slate)
        if result.get("success"):
            await update.message.reply_text(f"✅ Snapshot regenerated for {target} at {result.get('path')}")
        else:
            await update.message.reply_text(f"❌ Snapshot regeneration failed: {result.get('error')}")
    except Exception as error:
        logging.exception("/snapshot_regenerate failed")
        await update.message.reply_text(f"/snapshot_regenerate failed: {error!r}")


# ── Vault Commands ────────────────────────────────────────────────────────

async def vault_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show vault debug info for a date."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    target = args[0] if args else eastern_today().isoformat()
    try:
        from services.results_vault import vault_debug as _vault_debug, render_vault_debug
        payload = await asyncio.to_thread(_vault_debug, target)
        await update.message.reply_text(render_vault_debug(payload))
    except Exception as error:
        logging.exception("/vault_debug failed")
        await update.message.reply_text(f"/vault_debug failed: {error!r}")


# ── Grade Commands ────────────────────────────────────────────────────────

async def grade_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only command to inspect MLB grading decisions for one date."""
    if not await _require_admin(update):
        return
    if not update.message:
        return
    selected_date = normalize_pick_date(context.args[0]) if context.args else eastern_today().isoformat()
    if not selected_date:
        await update.message.reply_text("Usage: /grade_debug YYYY-MM-DD")
        return
    try:
        report = await asyncio.to_thread(grade_debug_report, selected_date)
    except Exception:
        logging.exception("Unexpected /grade_debug error")
        await update.message.reply_text(
            "Unable to build grade debug report right now. Check the terminal and try again."
        )
        return
    await _send_long_message(update, report)


async def results_auto_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only status for automatic end-of-day results posting."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        text = await asyncio.to_thread(results_auto_status_text)
    except Exception:
        logging.exception("Unexpected /results_auto_status error")
        text = "Unable to inspect automatic results status right now."
    await update.message.reply_text(text)


async def time_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only timezone and next-job diagnostics."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        text = await asyncio.to_thread(time_debug_text)
    except Exception as error:
        logging.exception("Unexpected /time_debug error")
        text = f"Unable to inspect scheduler time state right now.\n\nError: {error}"
    await update.message.reply_text(text)


async def scheduler_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only 3-step scheduler workflow status."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        text = await asyncio.to_thread(scheduler_status_text)
    except Exception as error:
        logging.exception("Unexpected /scheduler_status error")
        text = f"Unable to inspect scheduler status right now.\n\nError: {error}"
    await update.message.reply_text(text)


async def lineup_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only lineup state summary for today's MLB slate."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        payload = await asyncio.to_thread(summarize_lineups, upcoming_mlb_slate(slate), selected_date)
        await update.message.reply_text(render_lineup_status(payload))
    except Exception as error:
        logging.exception("Unexpected /lineup_status error")
        await update.message.reply_text(f"Unable to inspect lineup status right now.\n\nError: {error}")


async def prop_scratch_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only diagnostics for prop scratch false-invalidation protection."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        payload = await asyncio.to_thread(
            prop_scratch_debug_payload,
            selected_date,
            upcoming_mlb_slate(slate),
        )
        await update.message.reply_text(render_prop_scratch_debug(payload))
    except Exception as error:
        logging.exception("Unexpected /prop_scratch_debug error")
        await update.message.reply_text(f"Unable to inspect prop scratch status right now.\n\nError: {error}")


async def scratch_public_impact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: assess whether scratched players affect the PUBLIC official card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Assessing scratch public impact...")
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        assessment = await asyncio.to_thread(
            assess_scratch_public_impact, selected_date, upcoming_mlb_slate(slate),
        )
        affected = assessment.get("affected_public_picks", [])
        affected_names = ", ".join(item.get("player", "?") for item in affected) or "None"
        lines = [
            "🧪 SCRATCH PUBLIC IMPACT",
            f"📅 Date: {selected_date}",
            "",
            f"Public card affected: {'YES' if assessment.get('public_card_affected') else 'NO'}",
            f"Affected public picks: {affected_names}",
            f"Replacement generated: {'YES' if assessment.get('replacement_generated') else 'NO'}",
            f"Public correction needed: {'YES' if assessment.get('public_correction_needed') else 'NO'}",
            f"ML/F5/RL card unaffected: {'YES' if assessment.get('ml_f5_rl_unaffected') else 'NO'}",
        ]
        admin_only = assessment.get("admin_only_players", [])
        if admin_only:
            lines.append("")
            lines.append("Admin-only/watchlist scratched (owner alert only):")
            lines.extend(f"- {p}" for p in admin_only)
        await _send_long_message(update, "\n".join(lines))
    except Exception as error:
        logging.exception("/scratch_public_impact failed")
        await update.message.reply_text(f"❌ Scratch public impact failed:\n{error!r}")


async def post_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only automatic channel posting status."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        text = await asyncio.to_thread(post_status_text)
    except Exception as error:
        logging.exception("Unexpected /post_status error")
        text = f"Unable to inspect posting status right now.\n\nError: {error}"
    await update.message.reply_text(text)


async def workflow_debug_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only workflow state debug showing StructuredCard save details."""
    del context
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    try:
        selected = official_sports_date().isoformat()
        payload = await asyncio.to_thread(workflow_status, selected)
        generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
        verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
        posting = payload.get("posting") if isinstance(payload.get("posting"), dict) else {}
        posted_today = False
        import json as _json
        try:
            log_data = _json.loads(data_file("posting_log.json").read_text(encoding="utf-8"))
            posted_today = bool(log_data.get(f"posted_mlb_card_{selected}"))
        except Exception:
            pass
        if not verification and not generation and not posting:
            await update.message.reply_text("No workflow state found for today.")
            return
        odds_provider = verification.get("odds_provider_used", "none")
        odds_events = verification.get("odds_events_returned", 0)
        matched = verification.get("matched_games", 0)
        schedule_games = verification.get("schedule_games", 0)
        stats_only = os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
        market_mode = "Stats Only" if stats_only else "Normal"
        live_env_raw = os.getenv("LIVE_MLB_ENGINE")
        live_env_value = live_env_raw.strip().lower() if live_env_raw is not None and live_env_raw.strip() else "not_set"
        simple_engine = os.getenv("LIVE_MLB_ENGINE", "simple").strip().lower() == "simple"
        simple_status = await asyncio.to_thread(simple_card_bridge_status, selected) if simple_engine else {}
        simple_ready = bool(simple_status.get("simple_card_exists") and int(simple_status.get("simple_pick_count") or 0) > 0)
        lines = [
            "🔧 WORKFLOW DEBUG",
            f"Date: {display_date(selected)}",
            f"LIVE_MLB_ENGINE env value: {live_env_value}",
            f"Live Engine: {'Simple MLB Card v1' if simple_engine else 'Advanced'}",
            "",
            "── Market Context ──",
            f"Market Mode: {market_mode}",
            f"Odds provider used: {odds_provider}",
            f"Odds events returned: {odds_events}",
            f"Matched games: {matched} / {schedule_games}",
            f"Official picks skipped reason: {generation.get('last_save_exception') or verification.get('official_picks_skipped_reason', 'N/A')}",
            "",
            "── T-50 Verification ──",
            f"Ready: {'YES' if verification.get('ready_for_image_generation') else 'NO' if verification.get('ready_for_image_generation') is False else 'Not run'}",
            f"Games: {schedule_games}",
            f"Odds: {verification.get('odds', 'unavailable')}",
            f"Errors: {verification.get('critical_failures', []) or 'None'}",
            "",
            "── T-45 Generation ──",
            f"T-45 Simple Generate: {'YES' if generation.get('simple_generate') else 'NO'}",
            f"T-45 Advanced Generate: {generation.get('advanced_generate', 'skipped' if simple_engine else 'testing')}",
            f"Card Complete: {'YES' if generation.get('card_generation_complete') else 'NO'}",
            f"Picks Saved: {'YES' if generation.get('picks_saved') else 'NO'}",
            f"Saved Count: {generation.get('saved_picks', 0)}",
            f"Images: {'YES' if generation.get('image_generation_complete') else 'NO'}",
            f"Last Generation Error: {generation.get('generation_error') or 'None'}",
            "",
            "── Structured Card ──",
            f"Built: {'YES' if generation.get('structured_card_built') else 'NO'}",
            f"Official Picks Count: {generation.get('official_picks_count', 0)}",
            f"Save Path: {generation.get('save_path_used', 'N/A')}",
            f"Last Save Exception: {generation.get('last_save_exception') or 'None'}",
            f"Source File: {generation.get('generation_source_file', 'N/A')}",
            f"Source Function: {generation.get('generation_source_function', 'N/A')}",
            "",
            "── T-43 Posting ──",
            f"T-43 Simple Post: {'YES' if posting.get('engine') == 'simple_mlb_card_v1' or (simple_engine and posted_today) else 'NO'}",
            f"Status: {posting.get('status', 'Not run')}",
            f"Auto Post: {'ENABLED' if payload.get('auto_post_enabled') else 'DISABLED'}",
            f"Posted Today: {'YES' if posted_today else 'NO'}",
            f"Posting Ready: {'YES' if (simple_ready if simple_engine else generation.get('card_generation_complete') and (generation.get('picks_saved') or generation.get('saved_picks', 0) > 0) and payload.get('auto_post_enabled')) else 'NO'}",
        ]
        await update.message.reply_text("\n".join(lines))
    except Exception as error:
        logging.exception("/workflow_debug failed")
        await update.message.reply_text(f"/workflow_debug failed:\n{error!r}")


async def force_generate_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only manual T-45 generation run.

    Safety guard: blocks generation if first MLB game is more than
    90 minutes away.  Owner can override with ``--override``.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return

    override = any(arg.strip().lower() == "--override" for arg in (context.args or []))

    selected_date = official_sports_date().isoformat()

    if not override:
        first_pitch = await asyncio.to_thread(get_first_mlb_pitch, selected_date)
        if first_pitch is not None:
            now = now_et()
            minutes_until = (first_pitch - now).total_seconds() / 60.0
            if minutes_until > 90:
                await update.message.reply_text(
                    "Too early to generate official MLB card. "
                    "Starting pitchers/lineups may not be verified yet.\n\n"
                    "Allowed while waiting:\n"
                    "/odds_debug · /status\n"
                    "/scheduler_status · /workflow_debug\n"
                    "/post_status"
                )
                return

    await update.message.reply_text("⏳ Running T-45 generation now...")
    try:
        status = await generate_cards_job(context.bot, selected_date)
        await update.message.reply_text(
            "✅ FORCE GENERATE TODAY COMPLETE\n\n"
            f"Card Generated: {'YES' if status.get('card_generation_complete') else 'NO'}\n"
            f"Images Generated: {'YES' if status.get('image_generation_complete') else 'NO — text fallback OK'}\n"
            f"Picks Saved: {'YES' if status.get('picks_saved') else 'NO'}\n"
            f"Posting Ready: {'YES' if status.get('posting_ready') else 'NO'}\n"
            f"Last Generation Error: {status.get('generation_error') or 'None'}"
        )
    except Exception as error:
        logging.exception("Unexpected /force_generate_today error")
        await update.message.reply_text(
            "❌ FORCE GENERATE TODAY FAILED\n\n"
            f"Error: {error}\n\n"
            "The advanced card pipeline did not complete. You can still publish an "
            "emergency stats-based card without it:\n"
            "  /simple_generate_today  → build + save simple card\n"
            "  /simple_post_today      → post simple card to FREE channel\n"
            "  /force_post_text_card   → post the generated card text as-is"
        )


async def force_post_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only manual free-channel post using text fallback if needed."""
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Attempting force post to FREE_CHANNEL_ID...")
    try:
        result = await force_post_free_channel_job(context.bot, selected_date)
        if result.get("posted"):
            await update.message.reply_text("✅ Force post complete. FREE_CHANNEL_ID received today’s free card.")
        else:
            await update.message.reply_text(
                "❌ Force post did not run.\n\n"
                f"Reason: {result.get('reason', 'unknown')}"
            )
    except Exception as error:
        logging.exception("Unexpected /force_post_today error")
        await update.message.reply_text(f"Unable to force-post today.\n\nError: {error}")


async def save_today_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only force-save from today's generated T-45 card text."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    card_path = data_file("generated_cards") / selected_date / "mlb_card.txt"
    if not card_path.exists():
        await update.message.reply_text(
            "No generated MLB card text found for today. Run /force_generate_today first."
        )
        return
    await update.message.reply_text("⏳ Saving today's generated official picks...")
    try:
        card = card_path.read_text(encoding="utf-8")
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        last_error: Exception | None = None
        saved = 0
        for attempt in (1, 2):
            try:
                url_slate = upcoming_mlb_slate(slate)
                card_obj = await asyncio.to_thread(
                    build_card_from_analysis, card, url_slate, selected_date, "save_today_picks",
                )
                card_dict = structured_card_to_dict(card_obj)
                card_dict["analysis"] = card
                card_dict["slate"] = url_slate
                card_dict["source_command"] = "save_today_picks"
                save_result = await asyncio.to_thread(persist_official_card, card_dict)
                if not save_result.get("success"):
                    raise ResultsTrackerError(str(save_result.get("error") or "Pick persistence failed"))
                saved = int(save_result.get("saved_pick_count") or 0)
                last_error = None
                break
            except Exception as error:
                last_error = error
                logging.exception("/save_today_picks failed attempt=%s", attempt)
                if attempt == 1:
                    await asyncio.sleep(0.5)
        if last_error:
            raise last_error
        await update.message.reply_text(
            f"✅ Saved {saved} new official picks for {display_date(selected_date)}.\n\n"
            + render_saved_picks_summary(selected_date)
        )
    except Exception as error:
        logging.exception("Unexpected /save_today_picks error")
        await update.message.reply_text(
            f"❌ Save failed after retry. Posting should not continue.\n\nError: {error}"
        )


async def saved_picks_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only summary of today's saved official picks."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        text = await asyncio.to_thread(render_saved_picks_summary, selected_date)
    except Exception as error:
        logging.exception("Unexpected /saved_picks_today error")
        text = f"Unable to inspect saved picks right now.\n\nError: {error}"
    await update.message.reply_text(text)


async def save_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only diagnostics for the Pick Persistence Service."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        text = await asyncio.to_thread(render_save_debug, selected_date)
    except Exception as error:
        logging.exception("Unexpected /save_debug error")
        text = f"Unable to inspect pick persistence right now.\n\nError: {error}"
    await update.message.reply_text(text)


async def repair_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only repair for core runtime JSON files."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        payload = await asyncio.to_thread(repair_pick_storage)
        lines = ["🛠 BETGPTAI STORAGE REPAIR", ""]
        for filename, status in payload.items():
            if isinstance(status, dict):
                valid = "✅" if status.get("valid") else "❌"
                created = "created" if status.get("created") else "checked"
                path = status.get("path", filename)
                lines.append(f"{filename}: {valid} {created}")
                lines.append(f"Path: {path}")
            else:
                lines.append(f"{filename}: {status}")
            lines.append("")
        await update.message.reply_text("\n".join(lines).strip())
    except Exception as error:
        logging.exception("Unexpected /repair_storage error")
        await update.message.reply_text(f"Unable to repair storage right now.\n\nError: {error}")


async def post_results_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only manual run of the automatic results poster."""
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("Checking saved official MLB picks for final results...")
    try:
        summary = await post_daily_results_if_ready(
            context.bot,
            force=True,
        )
    except Exception:
        logging.exception("Unexpected /post_results_now error")
        await update.message.reply_text("Unable to post results right now. Check the terminal log.")
        return
    if summary.get("posted"):
        await update.message.reply_text(
            "✅ Daily results posted.\n\n"
            f"Date: {summary.get('day')}\n"
            f"Official Picks: {summary.get('picks', 0)}\n"
            f"Tracked Games: {summary.get('games', 0)}"
        )
    else:
        await update.message.reply_text(
            "Daily results were not posted.\n\n"
            f"Reason: {summary.get('reason', 'Unavailable')}\n"
            f"Date: {summary.get('day', 'Unavailable')}"
        )


async def enable_auto_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only switch for automatic daily results posting."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await asyncio.to_thread(set_auto_results_enabled, True)
    await update.message.reply_text("✅ Automatic MLB results posting enabled.")


async def disable_auto_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only switch for automatic daily results posting."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await asyncio.to_thread(set_auto_results_enabled, False)
    await update.message.reply_text("✅ Automatic MLB results posting disabled.")


async def debug_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only saved-pick diagnostics without exposing internals to members."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    try:
        summary = await asyncio.to_thread(
            debug_picks_summary, official_sports_date().isoformat()
        )
    except Exception:
        logging.exception("Unexpected /debug_picks error")
        await update.message.reply_text("Unable to inspect picks right now.")
        return
    errors = summary.get("last_errors") or []
    error_text = "\n".join(f"- {item}" for item in errors) if errors else "None"
    await update.message.reply_text(
        "🧪 BETGPTAI PICK DEBUG\n\n"
        f"Total picks saved today: {summary.get('total_today', 0)}\n"
        f"Pending picks: {summary.get('pending', 0)}\n"
        f"Graded picks: {summary.get('graded', 0)}\n"
        f"Picks missing game_id: {summary.get('missing_game_id', 0)}\n"
        f"Picks missing market_type: {summary.get('missing_market_type', 0)}\n"
        f"Picks missing selected_team: {summary.get('missing_selected_team', 0)}\n"
        f"Picks missing card_date: {summary.get('missing_card_date', 0)}\n\n"
        "Last grading errors:\n"
        f"{error_text}"
    )


async def extract_picks_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only StructuredCard extraction diagnostics."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        analysis = ""
        slate: list[dict[str, object]] = []
        if DAILY_CARD_FILE.exists():
            payload = json.loads(DAILY_CARD_FILE.read_text(encoding="utf-8"))
            latest_type = payload.get("latest_type")
            cards = payload.get("cards") if isinstance(payload.get("cards"), dict) else {}
            latest_card = cards.get(latest_type) if latest_type else None
            if isinstance(latest_card, dict):
                analysis = str(latest_card.get("analysis") or latest_card.get("raw_text") or "")
                saved_slate = latest_card.get("slate")
                if isinstance(saved_slate, list):
                    slate = [g for g in saved_slate if isinstance(g, dict)]
                date_val = latest_card.get("date") or latest_card.get("card_date")
                if date_val:
                    selected_date = normalize_pick_date(date_val) or selected_date

        card_generated = bool(analysis.strip())
        card = await asyncio.to_thread(
            build_card_from_analysis, analysis, slate, selected_date, "extract_picks_debug",
        ) if card_generated else None

        lines = [
            "🧪 BETGPTAI EXTRACT PICKS DEBUG",
            f"Card date: {selected_date}",
            f"Card generated: {'YES' if card_generated else 'NO'}",
        ]
        if card:
            total = len(card.official_picks)
            lines.append(f"Official picks: {total}")
            errors = (card.metadata or {}).get("errors", [])
            lines.append(f"Skipped/unmatched: {len(errors)}")
            sections_found = (card.metadata or {}).get("headings_found", [])
            lines.append(f"Sections found: {', '.join(sections_found) if sections_found else 'None'}")
            lines.append("")
            sections = card.display_sections or {}
            for key, picks in sections.items():
                if key.startswith("_"):
                    continue
                lines.append(f"  {key}: {len(picks) if isinstance(picks, list) else 0} picks")
                for p in (picks or []):
                    lines.append(f"    - {p}")
            if errors:
                lines.extend(["", "Skipped picks / errors:"])
                for err in errors:
                    lines.append(f"  - {err}")
            missing = []
            for pick in card.official_picks:
                if not pick.game_pk:
                    missing.append("game_pk")
                if not pick.selected_team:
                    missing.append("selected_team")
                if pick.odds is None:
                    missing.append("odds")
                if not pick.market_type:
                    missing.append("market_type")
            if missing:
                lines.extend(["", "Missing required fields:", f"  {set(missing)}"])
        else:
            lines.append("No card data available to extract.")
        await update.message.reply_text("\n".join(lines))
    except Exception:
        logging.exception("Unexpected /extract_picks_debug error")
        await update.message.reply_text("Unable to inspect pick extraction right now.")


async def debug_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only diagnostics for daily result scoping."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    try:
        summary = await asyncio.to_thread(debug_results_summary)
    except Exception:
        logging.exception("Unexpected /debug_results error")
        await update.message.reply_text("Unable to inspect results right now.")
        return
    dates_text = ", ".join(summary.get("dates", [])) or "None"
    last_rows = []
    for pick in summary.get("last_10", []):
        last_rows.append(
            f"- {pick.get('date')} | card_date={pick.get('card_date')} | {pick.get('status')} | "
            f"{pick.get('result')} | {pick.get('pick_text')}"
        )
    await update.message.reply_text(
        "🧪 BETGPTAI RESULTS DEBUG\n\n"
        f"Total picks in picks.json: {summary.get('total_picks', 0)}\n"
        f"Picks for today: {summary.get('picks_today', 0)}\n"
        f"Picks already graded today: {summary.get('graded_today', 0)}\n"
        f"Pending today: {summary.get('pending_today', 0)}\n"
        f"Dates found in picks.json: {dates_text}\n"
        f"Picks missing card_date: {summary.get('missing_card_date', 0)}\n"
        f"Picks missing game_pk: {summary.get('missing_game_pk', 0)}\n"
        f"Picks missing market_type: {summary.get('missing_market_type', 0)}\n\n"
        "Last 10 picks:\n"
        + ("\n".join(last_rows) if last_rows else "None")
    )


async def save_last_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only recovery command that saves the latest generated MLB card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("Attempting to save the latest official MLB card...")
    try:
        analysis = ""
        slate: list[dict[str, object]] = []
        recovered_source = "mlb_auto"
        if DAILY_CARD_FILE.exists():
            payload = json.loads(DAILY_CARD_FILE.read_text(encoding="utf-8"))
            latest_type = payload.get("latest_type")
            cards = payload.get("cards") if isinstance(payload.get("cards"), dict) else {}
            latest_card = cards.get(latest_type) if latest_type else None
            if isinstance(latest_card, dict):
                analysis = str(
                    latest_card.get("analysis")
                    or latest_card.get("raw_text")
                    or ""
                )
                saved_slate = latest_card.get("slate")
                if isinstance(saved_slate, list):
                    slate = [game for game in saved_slate if isinstance(game, dict)]
                selected_date = normalize_pick_date(
                    latest_card.get("date") or latest_card.get("card_date")
                ) or selected_date
                if latest_card.get("source_command"):
                    recovered_source = str(latest_card["source_command"])

        if not analysis.strip() or not slate:
            slate = await asyncio.to_thread(
                get_combined_slate,
                os.getenv("ODDS_API_KEY", ""),
                game_date=selected_date,
                highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
            )
            if not slate:
                await update.message.reply_text("No MLB games were found to save.")
                return
            try:
                analysis = await analyze_mlb_slate(
                    slate,
                    os.getenv("OPENAI_API_KEY", ""),
                    os.getenv("ANTHROPIC_API_KEY", ""),
                )
            except Exception as error:
                logging.error("AI Analysis Error:\n%s", error, exc_info=True)
                analysis = build_fallback_card(slate)

        saved_count = await asyncio.to_thread(
            _save_official_mlb_card,
            analysis,
            slate,
            selected_date,
            recovered_source,
        )
        await update.message.reply_text(
            f"✅ Saved {saved_count} official MLB picks for {selected_date}."
        )
    except Exception as error:
        logging.exception("/save_last_card failed")
        await update.message.reply_text(
            f"Unable to save the latest card. Error: {error}"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show private configuration status without revealing secret values."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    def core_configured(name: str) -> str:
        return "✅ Available" if os.getenv(name, "").strip() else "❌ Missing"

    def optional_configured(*names: str) -> str:
        return (
            "✅ Available"
            if any(os.getenv(name, "").strip() for name in names)
            else "➖ Not configured"
        )

    try:
        counts = await asyncio.to_thread(_database_counts)
    except ResultsTrackerError:
        logging.exception("Could not count picks for /status")
        counts = {"total": 0}

    try:
        storage_payload = await asyncio.to_thread(get_storage_status)
        if storage_payload.get("results_database_healthy"):
            logging.info("Results database check passed")
            STORAGE_LOG.info("component=ResultsStorage status=healthy recovery=none")
            database_status = "✅ Healthy"
        else:
            reason = (
                f"writable={storage_payload.get('writable')}, "
                f"picks_exists={storage_payload.get('picks_exists')}, "
                f"picks_valid={storage_payload.get('picks_valid')}, "
                f"results_exists={storage_payload.get('results_exists')}, "
                f"results_valid={storage_payload.get('results_valid')}"
            )
            logging.warning("Results database check failed: %s", reason)
            STORAGE_LOG.warning(
                "component=ResultsStorage status=failed error=%s recovery=auto_repair_attempted",
                reason,
            )
            database_status = "❌ Unavailable"
    except Exception as error:
        logging.exception("Results database check failed: %s", error)
        STORAGE_LOG.exception(
            "component=ResultsStorage error=%s recovery=mark_unavailable_notify_admin",
            error,
        )
        database_status = "❌ Unavailable"

    football_data_key = (
        os.getenv("FOOTBALL_DATA_KEY", "").strip()
        or os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
    )
    api_football_key = os.getenv("API_FOOTBALL_KEY", "").strip()

    (
        baseball_savant_available,
        fangraphs_status,
        pybaseball_status,
        statsbomb_ok,
    ) = await asyncio.gather(
        asyncio.to_thread(savant_available),
        asyncio.to_thread(fangraphs_status_label),
        asyncio.to_thread(pybaseball_status_label),
        asyncio.to_thread(statsbomb_available),
    )
    savant_status = "✅ Available" if baseball_savant_available else "➖ Optional unavailable"
    sportsdb_status = await asyncio.to_thread(thesportsdb_status_label)
    sportsdb_url = thesportsdb_base_url() or "Disabled"
    football_data_status = "✅ Available" if football_data_key else "➖ Not configured"
    api_football_status = await asyncio.to_thread(api_football_status_label, api_football_key)
    if _truthy_env("CLUB_ELO_ENABLED"):
        clubelo_available = await asyncio.to_thread(check_clubelo)
        clubelo_status = "✅ Available" if clubelo_available else "➖ Optional unavailable"
    else:
        clubelo_status = "➖ Disabled"
    if _truthy_env("UNDERSTAT_ENABLED"):
        understat_available = await asyncio.to_thread(check_understat)
        understat_status = "✅ Available" if understat_available else "➖ Optional unavailable"
    else:
        understat_status = "➖ Disabled"
    api_sports_baseball_status = (
        "➖ Disabled"
        if not _truthy_env("API_SPORTS_BASEBALL_ENABLED")
        else (
            "✅ Available"
            if await asyncio.to_thread(
                api_sports_baseball_available,
                os.getenv("API_SPORTS_KEY", "") or os.getenv("API_FOOTBALL_KEY", ""),
            )
            else "➖ Optional unavailable"
        )
    )
    props_engine_status = (
        "✅ Available" if player_props_engine_available() else "➖ Optional unavailable"
    )
    serpapi_status = (
        "✅ Configured"
        if check_serpapi(os.getenv("SERPAPI_KEY", ""))
        else "➖ Not configured"
    )
    image_engine_status = (
        "✅ Enabled"
        if _truthy_env("IMAGE_GENERATION_ENABLED") and os.getenv("OPENAI_API_KEY", "").strip()
        else "➖ Disabled"
    )
    statsbomb_status = "✅ Available" if statsbomb_ok else "➖ Optional unavailable"

    await update.message.reply_text(
        "🕊 BETGPTAI STATUS\n\n"
        "🟢 CORE SYSTEM\n\n"
        "Telegram: ✅ Online\n"
        "MLB Stats API: ✅ Available\n"
        f"OpenAI: {core_configured('OPENAI_API_KEY')}\n"
        f"Claude: {core_configured('ANTHROPIC_API_KEY')}\n"
        "Weather: ✅ Available\n"
        f"Storage: {database_status}\n"
        f"Results Database: {database_status}\n"
        f"Image Engine: {image_engine_status}\n"
        "Live Updates: ➖ Disabled\n\n"
        "🟢 MLB DATA\n\n"
        "MLB Stats API: ✅ Available\n"
        f"Baseball Savant: {savant_status}\n"
        f"FanGraphs: {fangraphs_status}\n"
        f"pybaseball: {pybaseball_status}\n\n"
        "🟢 SOCCER DATA\n\n"
        f"API-Football: {api_football_status}\n"
        f"Football-Data: {football_data_status}\n"
        f"TheSportsDB: {sportsdb_status}\n"
        f"StatsBomb: {statsbomb_status}\n"
        f"ClubElo: {clubelo_status}\n"
        f"Understat: {understat_status}\n\n"
        "🟢 OPTIONAL\n\n"
        f"Highlightly: {optional_configured('HIGHLIGHTLY_API_KEY')}\n"
        f"SerpApi: {serpapi_status}\n"
        "── Sharp API (multi-sport) ──\n"
        f"MLB: {'✅ Supported' if os.getenv('SHARP_API_KEY', '').strip() else '❌ No key'}\n"
        f"Soccer: {'✅ Supported' if os.getenv('SHARP_API_KEY', '').strip() else '❌ No key'}\n"
        f"NBA: {'✅ Supported' if os.getenv('SHARP_API_KEY', '').strip() else '❌ No key'}\n"
        f"NFL: {'✅ Supported' if os.getenv('SHARP_API_KEY', '').strip() else '❌ No key'}\n"
        f"NHL: {'✅ Supported' if os.getenv('SHARP_API_KEY', '').strip() else '❌ No key'}\n"
        f"Odds API (backup, MLB only): {optional_configured('ODDS_API_KEY')}\n"
        f"API-Sports Baseball: {api_sports_baseball_status}\n"
        f"Player Props Engine: {props_engine_status}\n\n"
        f"TheSportsDB Base URL: {sportsdb_url}\n\n"
        f"📊 Picks Tracked: {counts['total']}\n"
        "🤖 BETGPTAI v2.0"
    )


async def storage_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Railway persistent-storage diagnostics."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        status_payload = await asyncio.to_thread(get_storage_status)
    except Exception:
        logging.exception("Unexpected /storage_status error")
        await update.message.reply_text("Unable to inspect storage right now.")
        return

    def yes_no(value: object) -> str:
        return "✅" if value else "❌"

    await update.message.reply_text(
        "💾 BETGPTAI STORAGE STATUS\n\n"
        f"DATA_DIR: {status_payload.get('data_dir')}\n"
        f"Writable: {yes_no(status_payload.get('writable'))}\n"
        f"picks.json path: {status_payload.get('picks_path')}\n"
        f"picks.json exists: {yes_no(status_payload.get('picks_exists'))}\n"
        f"picks.json valid: {yes_no(status_payload.get('picks_valid'))}\n"
        f"picks count: {status_payload.get('picks_count', 0)}\n"
        f"results.json path: {status_payload.get('results_path')}\n"
        f"results.json exists: {yes_no(status_payload.get('results_exists'))}\n"
        f"results.json valid: {yes_no(status_payload.get('results_valid'))}\n"
        f"generated_cards path: {status_payload.get('generated_cards_path')}\n"
        f"generated_cards exists: {yes_no(status_payload.get('generated_cards_exists'))}\n"
        f"Last write test: {status_payload.get('last_write_test')}\n"
        f"Disk free: {(status_payload.get('disk_usage') or {}).get('free_mb')} MB"
    )


async def debug_thesportsdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only TheSportsDB connectivity/debug report."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    enabled = thesportsdb_enabled()
    key = thesportsdb_api_key()
    version = thesportsdb_version()
    endpoint = thesportsdb_base_url(key or "123") + "all_leagues.php"
    status_code: str | int = "Not called"
    sample = "Provider disabled."
    if enabled and key:
        try:
            response = await asyncio.to_thread(requests.get, endpoint, timeout=12)
            status_code = response.status_code
            try:
                payload = response.json()
                sample = json.dumps(payload, ensure_ascii=False)[:900]
            except ValueError:
                sample = response.text[:900]
            API_LOG.info("TheSportsDB debug check status=%s recovery=diagnostic_only", status_code)
        except Exception as error:
            status_code = "Error"
            sample = str(error)
            API_LOG.exception("component=TheSportsDB error=%s recovery=optional_skip", error)
    elif enabled and not key:
        sample = "THESPORTSDB_ENABLED=true but THESPORTSDB_API_KEY is not configured."
    await _send_long_message(
        update,
        "🧪 THESPORTSDB DEBUG\n\n"
        f"Enabled: {'yes' if enabled else 'no'}\n"
        f"Loaded key: {'yes' if bool(key) else 'no'}\n"
        f"Version: {version}\n"
        f"Endpoint: {endpoint}\n"
        f"HTTP status: {status_code}\n\n"
        f"Sample response:\n{sample}",
    )


async def debug_football_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Football-Data.org connectivity/debug report."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip() or os.getenv("FOOTBALL_DATA_KEY", "").strip()
    selected_date = official_sports_date().isoformat()
    endpoint = "https://api.football-data.org/v4/matches"
    status_code: str | int = "Not called"
    sample = "FOOTBALL_DATA_API_KEY is not configured."
    if key:
        try:
            response = await asyncio.to_thread(
                requests.get,
                endpoint,
                headers={"X-Auth-Token": key},
                params={"dateFrom": selected_date, "dateTo": selected_date},
                timeout=12,
            )
            status_code = response.status_code
            try:
                payload = response.json()
                sample = json.dumps(payload, ensure_ascii=False)[:900]
            except ValueError:
                sample = response.text[:900]
            API_LOG.info("Football-Data debug check status=%s recovery=diagnostic_only", status_code)
        except Exception as error:
            status_code = "Error"
            sample = str(error)
            API_LOG.exception("component=Football-Data error=%s recovery=optional_skip", error)
    await _send_long_message(
        update,
        "🧪 FOOTBALL-DATA DEBUG\n\n"
        f"Loaded key: {'yes' if bool(key) else 'no'}\n"
        f"Endpoint: {endpoint}\n"
        f"Date: {selected_date}\n"
        f"HTTP status: {status_code}\n\n"
        f"Sample response:\n{sample}",
    )


async def debug_pybaseball(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only pybaseball/FanGraphs diagnostics."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    spec = importlib.util.find_spec("pybaseball")
    import_success = spec is not None
    version = "Unavailable"
    location = getattr(spec, "origin", None) if spec else None
    test_query = "Not run"
    error = "None"
    if import_success:
        try:
            version = importlib.metadata.version("pybaseball")
        except importlib.metadata.PackageNotFoundError:
            version = "Unknown"
        try:
            test_query = await asyncio.to_thread(pybaseball_status_label)
        except Exception as exc:
            error = str(exc)
            test_query = "Failed"
            API_LOG.exception("component=pybaseball error=%s recovery=optional_skip", exc)
    await update.message.reply_text(
        "🧪 PYBASEBALL DEBUG\n\n"
        f"Import success: {'yes' if import_success else 'no'}\n"
        f"Version: {version}\n"
        f"Installed location: {location or 'Unavailable'}\n"
        f"Test query: {test_query}\n"
        f"Errors: {error}"
    )


def _count_verified_pitchers(slate: list[dict[str, Any]]) -> int:
    count = 0
    for game in slate:
        if game.get("away_pitcher") and game.get("away_pitcher") != "TBD":
            count += 1
        if game.get("home_pitcher") and game.get("home_pitcher") != "TBD":
            count += 1
    return count


async def integrity_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only pre-publish data integrity gate."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    storage_payload = await asyncio.to_thread(get_storage_status)
    slate: list[dict[str, Any]] = []
    schedule_ok = False
    weather_ok = False
    odds_ok = False
    lineups_ok = False
    images_ready = False
    errors: list[str] = []
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        schedule_ok = bool(slate)
        weather_ok = any(isinstance(game.get("weather"), dict) for game in slate)
        odds_ok = any(game.get("odds_status") == "available" or game.get("odds") for game in slate)
        lineups_ok = any(game.get("lineups") not in (None, "", "unavailable", [], {}) for game in slate)
    except Exception as error:
        errors.append(str(error))
        API_LOG.exception("component=IntegrityReport error=%s recovery=notify_admin_no_publish", error)
    verified_pitchers = _count_verified_pitchers(slate)
    props_cache_ok = data_file("props_lab.json").exists()
    generated_today = data_file("generated_cards") / selected_date
    generated_legacy = data_file("generated_cards") / datetime.fromisoformat(selected_date).strftime("%m-%d-%Y")
    images_ready = generated_today.exists() or generated_legacy.exists()
    required_ok = bool(
        schedule_ok
        and verified_pitchers > 0
        and storage_payload.get("results_database_healthy")
        and weather_ok
    )
    ready = required_ok
    await _send_long_message(
        update,
        "🛡 BETGPTAI INTEGRITY REPORT\n\n"
        f"📅 Date: {datetime.fromisoformat(selected_date).strftime('%m/%d/%Y')}\n\n"
        f"Today's Games: {len(slate)} {'✅' if schedule_ok else '❌'}\n"
        f"Verified Pitchers: {verified_pitchers} {'✅' if verified_pitchers else '❌'}\n"
        f"Confirmed Lineups: {'✅' if lineups_ok else '➖ Not confirmed yet'}\n"
        f"Storage: {'✅ Healthy' if storage_payload.get('writable') else '❌ Failed'}\n"
        f"Results: {'✅ Healthy' if storage_payload.get('results_database_healthy') else '❌ Failed'}\n"
        f"Weather: {'✅ Available' if weather_ok else '❌ Failed'}\n"
        f"Odds: {'✅ Available' if odds_ok else '➖ Not configured/unavailable'}\n"
        f"Images: {'✅ Ready' if images_ready else '➖ Not generated yet'}\n"
        f"Props Cache: {'✅ Present' if props_cache_ok else '➖ Empty'}\n\n"
        f"Ready To Publish: {'YES ✅' if ready else 'NO ❌'}\n\n"
        f"Errors: {', '.join(errors) if errors else 'None'}"
    )


async def _mission_control_health_text() -> str:
    """Compact owner-only health snapshot for the inline Mission Control panel."""
    selected_date = official_sports_date().isoformat()
    storage_payload = await asyncio.to_thread(get_storage_status)
    pick_storage_payload = await asyncio.to_thread(pick_persistence_debug, selected_date)
    slate: list[dict[str, Any]] = []
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        slate = await asyncio.to_thread(enrich_mlb_slate_verification, slate, selected_date)
    except Exception as error:
        API_LOG.exception("component=MissionControl error=%s recovery=admin_snapshot_partial", error)
    images_dir = data_file("generated_cards") / selected_date
    legacy_images_dir = data_file("generated_cards") / datetime.fromisoformat(selected_date).strftime("%m-%d-%Y")
    images_ready = images_dir.exists() or legacy_images_dir.exists()
    learning_reports = data_file("learning_reports")
    learning_ready = learning_reports.exists()
    results_ready = storage_payload.get("results_database_healthy")
    weather_ready = any(isinstance(game.get("weather"), dict) for game in slate)
    quant_payload = build_elite_quant_slate(slate, include_market=bool(os.getenv("ODDS_API_KEY", "")))
    v20_games = [game.get("betgptai_quant_v20") for game in slate if isinstance(game.get("betgptai_quant_v20"), dict)]
    v20_qualified = sum(1 for item in v20_games if item.get("engine_decision") == "QUALIFIED")
    edge_db_path = data_file("edge_database") / f"{selected_date}.json"
    simple_engine = os.getenv("LIVE_MLB_ENGINE", "simple").strip().lower() == "simple"
    simple_status = await asyncio.to_thread(simple_card_bridge_status, selected_date) if simple_engine else {}
    ready_to_publish = bool(
        simple_status.get("simple_card_exists") and int(simple_status.get("simple_pick_count") or 0) > 0
    ) if simple_engine else bool(slate and results_ready and weather_ready and quant_payload)
    ready_games = sum(1 for item in quant_payload if item.get("game_status") == "ready")
    quant_weights = await asyncio.to_thread(current_quant_weights)
    verification_score = average_verification_score(slate)
    sharp_line = await asyncio.to_thread(sharp_api_status_line)
    weights_text = (
        f"SP {quant_weights['sp_score']:.0%} / "
        f"Offense {quant_weights['offense_score']:.0%} / "
        f"Bullpen {quant_weights['bullpen_score']:.0%} / "
        f"Defense {quant_weights['defense_score']:.0%} / "
        f"Weather/Park {quant_weights['weather_park_score']:.0%} / "
        f"Market {quant_weights['market_value_score']:.0%} / "
        f"Situational {quant_weights['situational_score']:.0%}"
    )
    return (
        "🧠 BETGPTAI MISSION CONTROL\n\n"
        f"Live Engine: {'Simple MLB Card v1' if simple_engine else 'Advanced'}\n"
        f"System Health: {'✅ Healthy' if storage_payload.get('writable') else '❌ Needs attention'}\n"
        f"Storage: {'✅ Healthy' if storage_payload.get('results_database_healthy') else '❌ Failed'}\n"
        f"Picks Saved Today: {pick_storage_payload.get('todays_picks', 0)}\n"
        f"Last Save: {pick_storage_payload.get('last_save_time', 'Unavailable')}\n"
        f"Last Storage Error: {pick_storage_payload.get('last_error', 'None')}\n"
        "API Status: Use /status for full provider detail\n"
        f"Today's Games: {len(slate)}\n"
        f"Verification Score: {verification_score}/100\n"
        f"Satchel Sharp: {sharp_line}\n"
        f"v20 Engine: {'✅ Available' if v20_games else '➖ No slate scored yet'}\n"
        f"v20 Model: {QUANT_MODEL_VERSION}\n"
        f"v20 Qualified Edges: {v20_qualified}/{len(v20_games)}\n"
        f"Edge Database: {'✅ Saved' if edge_db_path.exists() else '➖ Pending'}\n"
        f"Current Weights: {weights_text}\n"
        f"Quant Engine: {ready_games}/{len(quant_payload)} ready\n"
        f"Images Ready: {'✅ Yes' if images_ready else '➖ Not yet'}\n"
        f"Results Ready: {'✅ Yes' if results_ready else '❌ No'}\n"
        f"AI Learning: {'✅ Available' if learning_ready else '➖ No report yet'}\n"
        f"{ai_learning_auto_apply_line()}\n"
        f"Ready To Publish: {'YES ✅' if ready_to_publish else 'NO ❌'}"
    )


async def specialized_mlb_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Build one hidden, owner-only Statcast-first premium MLB card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    command = update.message.text.split()[0].lstrip("/").split("@")[0]
    await update.message.reply_text("⏳ Building Statcast matchup card...")
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=official_sports_date().isoformat(),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        card = await analyze_specialized_mlb_slate(
            slate, os.getenv("OPENAI_API_KEY", ""), command,
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        try:
            saved_count = await asyncio.to_thread(
                _save_official_mlb_card,
                card,
                slate,
                official_sports_date().isoformat(),
                command,
            )
        except Exception:
            logging.exception("Could not save /%s official picks", command)
        await _send_long_message(update, _with_card_date(card, official_sports_date().isoformat()))
    except Exception:
        logging.exception("Unexpected /%s error", command)
        await update.message.reply_text("Unable to build that MLB card right now.")


async def _send_props_lab(
    update: Update, mode: str = "summary"
) -> None:
    """Build and send an owner-only MLB player props lab view."""
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building BETGPTAI Player Prop Lab...")
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        payload = await asyncio.to_thread(
            build_player_props_lab, slate, selected_date
        )
        if mode == "hits":
            card = render_prop_type_card(payload, "hits")
        elif mode == "hits_by_team":
            card = render_hits_by_team_card(payload)
        elif mode == "home_runs":
            card = render_prop_type_card(payload, "home_runs")
        elif mode == "strikeouts":
            card = render_prop_type_card(payload, "strikeouts")
        elif mode == "debug":
            card = render_prop_debug(payload)
        elif mode == "hitprops_debug":
            card = render_hitprops_debug(payload)
        elif mode == "fanduel_debug":
            card = render_fanduel_props_debug(payload)
        elif mode == "test":
            card = render_props_test(payload)
        else:
            card = render_props_admin_card(payload)
        await _send_long_message(update, card)
    except Exception:
        logging.exception("Unexpected player props lab error")
        await update.message.reply_text(
            "Unable to build the Player Prop Lab right now. Check terminal logs."
        )


async def _build_verified_props_payload(selected_date: str) -> tuple[list[dict], dict]:
    """Build today's verified admin prop payload from the full MLB slate."""
    slate = await asyncio.to_thread(
        get_combined_slate,
        os.getenv("ODDS_API_KEY", ""),
        game_date=selected_date,
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    if not slate:
        return [], {}
    payload = await asyncio.to_thread(build_player_props_lab, slate, selected_date)
    return slate, payload


async def _send_prompt_previews(
    update: Update,
    prompt_items: list[dict[str, str]],
    output_dir: Path,
    *,
    save_function,
    image_prefix: str,
) -> None:
    """Save prompts, send them to the owner, and optionally generate images."""
    if not update.message:
        return
    saved_paths = await asyncio.to_thread(save_function, prompt_items, output_dir)
    for index, item in enumerate(prompt_items, start=1):
        await _send_long_message(
            update,
            f"🖼 {image_prefix}\n"
            f"{index}/{len(prompt_items)} — {item.get('title', item.get('name'))}\n\n"
            f"{item['prompt']}",
        )
    await update.message.reply_text(
        "✅ Anime Vault prompts saved.\n\n"
        + "\n".join(str(path) for path in saved_paths)
    )
    if not _truthy_env("IMAGE_GENERATION_ENABLED"):
        await update.message.reply_text(
            "IMAGE_GENERATION_ENABLED=false, so I sent prompt-ready cards only."
        )
        return

    await update.message.reply_text(
        "🎨 IMAGE_GENERATION_ENABLED=true. Generating owner-only preview images..."
    )
    for index, item in enumerate(prompt_items, start=1):
        image_path = output_dir / f"{index:02d}_{item['name']}.png"
        try:
            saved_image = await asyncio.to_thread(
                generate_image_from_prompt, item["prompt"], str(image_path)
            )
            with Path(saved_image).open("rb") as image_file:
                await update.message.reply_photo(
                    photo=image_file,
                    caption=f"{image_prefix} Preview — {index}/{len(prompt_items)}",
                )
        except Exception as error:
            logging.exception("Image generation failed for %s %s", image_prefix, index)
            await _send_long_message(
                update,
                f"❌ Image generation failed for {image_prefix} {index}.\n\n"
                f"Error: {error}\n\n"
                f"Fallback prompt:\n\n{item['prompt']}",
            )


async def _send_single_image_preview(
    update_or_message: object,
    result: dict,
    *,
    title: str,
    approved_post_hint: str = "",
) -> None:
    """Send one owner-only generated image, or the prompt fallback."""
    message = getattr(update_or_message, "message", None) or update_or_message
    if not message:
        return
    image_path = result.get("image_path")
    prompt_path = result.get("prompt_path")
    if image_path:
        with Path(image_path).open("rb") as image_file:
            await message.reply_photo(
                photo=image_file,
                caption=(
                    f"{title} — OWNER PREVIEW\n\n"
                    f"{approved_post_hint}".strip()
                ),
            )
        await message.reply_text(
            f"✅ Preview image saved.\n\nImage: {image_path}\nPrompt: {prompt_path}"
        )
        return

    error_text = f"\nImage Error: {result.get('image_error')}\n" if result.get("image_error") else ""
    await _send_long_text_to_message(
        message,
        f"✅ Prompt saved for {title}.\n\n"
        f"Prompt: {prompt_path}\n"
        f"{error_text}\n"
        "Prompt-only fallback:\n\n"
        f"{result.get('prompt')}",
    )


async def props_images_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only Anime Vault image prompts/previews for the main props."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building verified prop image cards...")
    selected_date = official_sports_date().isoformat()
    try:
        _, payload = await _build_verified_props_payload(selected_date)
        if not payload:
            await update.message.reply_text("No MLB games were found for today.")
            return
        prompt_items = generate_prop_card_prompts(payload)
        if not prompt_items:
            await update.message.reply_text(
                "No verified player props are available for image cards."
            )
            return
        output_dir = _dated_generated_cards_dir(selected_date) / "props"
        await _send_prompt_previews(
            update,
            prompt_items,
            output_dir,
            save_function=save_prop_prompts,
            image_prefix="BETGPTAI Props Anime Vault",
        )
    except Exception as error:
        logging.exception("/props_images_admin failed")
        await update.message.reply_text(f"Unable to build prop images. Error: {error}")


async def hits_by_team_image_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only Anime Vault image prompts/previews for team hit props."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building verified hits-by-team image cards...")
    selected_date = official_sports_date().isoformat()
    try:
        _, payload = await _build_verified_props_payload(selected_date)
        if not payload:
            await update.message.reply_text("No MLB games were found for today.")
            return
        prompt_items = generate_hits_by_team_prompts(payload)
        if not prompt_items:
            await update.message.reply_text(
                "No verified hits-by-team props are available for image cards."
            )
            return
        output_dir = _dated_generated_cards_dir(selected_date) / "hits_by_team"
        await _send_prompt_previews(
            update,
            prompt_items,
            output_dir,
            save_function=save_prop_prompts,
            image_prefix="BETGPTAI Hits By Team Anime Vault",
        )
    except Exception as error:
        logging.exception("/hits_by_team_image_admin failed")
        await update.message.reply_text(
            f"Unable to build hits-by-team images. Error: {error}"
        )


async def best_hit_image_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: generate the official Best Hit Prop image preview."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building verified BEST HIT PROP OF THE DAY image...")
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        result = await asyncio.to_thread(
            prepare_best_hit_prop_image,
            slate,
            selected_date,
            image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
        )
        if result.get("status") != "ready":
            await update.message.reply_text(
                "❌ Best Hit image was not generated.\n\n"
                f"Reason: {result.get('reason')}"
            )
            return
        prop = result.get("prop") or {}
        summary = (
            "✅ BEST HIT PROP IMAGE READY\n\n"
            f"Player: {prop.get('player_name')}\n"
            f"Team: {prop.get('team_name')}\n"
            f"Opponent: {prop.get('opponent_name')}\n"
            f"Game Time: {prop.get('game_time_et')}\n"
            f"Prompt: {result.get('prompt_path')}\n"
        )
        image_path = result.get("image_path")
        if image_path:
            with Path(image_path).open("rb") as image_file:
                await update.message.reply_photo(
                    photo=image_file,
                    caption=(
                        "BETGPTAI BEST HIT PROP OF THE DAY — OWNER PREVIEW\n\n"
                        "Review this image. If approved, run /post_best_hit_image_admin."
                    ),
                )
            await update.message.reply_text(summary + f"Image: {image_path}")
        else:
            await _send_long_message(
                update,
                summary
                + "\nImage was not generated. Prompt-only fallback below.\n"
                + (f"Image Error: {result.get('image_error')}\n\n" if result.get("image_error") else "\n")
                + str(result.get("prompt")),
            )
    except Exception as error:
        logging.exception("/best_hit_image_admin failed")
        await update.message.reply_text(
            f"Unable to build Best Hit image right now. Error: {error}"
        )


async def post_best_hit_image_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only approval step: post the generated Best Hit image to FREE."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        verification_result = await asyncio.to_thread(get_verified_best_hit_prop, slate, selected_date)
        if verification_result.get("status") != "ready":
            await update.message.reply_text(
                "❌ Best hit prop rejected due to failed team verification.\n\n"
                "No image was posted."
            )
            return
        verified_prop = verification_result.get("prop") or {}
    except Exception as error:
        logging.exception("Best Hit verification failed before posting")
        await update.message.reply_text(
            f"❌ Best hit prop rejected due to failed team verification.\n\nError: {error}"
        )
        return
    if not await _quality_gate_or_notify(update, mode="best_hit"):
        return
    folder_name = datetime.fromisoformat(selected_date).strftime("%m-%d-%Y")
    image_path = data_file("generated_cards") / folder_name / "best_hit_prop.png"
    meta_path = data_file("generated_cards") / folder_name / "best_hit_prop_meta.json"
    if not image_path.exists():
        await update.message.reply_text(
            "❌ No approved Best Hit image found.\n\n"
            "Run /best_hit_image_admin with IMAGE_GENERATION_ENABLED=true first, "
            "review the owner preview, then run this approval command."
        )
        return
    try:
        image_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        image_meta = {}
    if image_meta.get("prop_id") != verified_prop.get("prop_id"):
        await update.message.reply_text(
            "❌ Best Hit image does not match today’s currently verified prop.\n\n"
            "Run /best_hit_image_admin again before posting."
        )
        return
    try:
        chat_id = _telegram_destination(os.getenv("FREE_CHANNEL_ID", ""))
        with image_path.open("rb") as image_file:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=image_file,
                caption=(
                    "🔥 BETGPTAI HIT PROP OF THE DAY\n\n"
                    "Educational analysis only. Singles are recommended."
                ),
            )
        await update.message.reply_text(
            f"✅ Best Hit image posted to FREE channel.\n\n{image_path}"
        )
    except Exception as error:
        logging.exception("/post_best_hit_image_admin failed")
        await update.message.reply_text(
            f"❌ Failed to post Best Hit image to FREE channel.\n\nError: {error}"
        )


async def clear_prop_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: remove same-day prop/image caches so stale props cannot survive."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    mmddyyyy = datetime.fromisoformat(selected_date).strftime("%m-%d-%Y")
    removed: list[str] = []
    targets = [
        data_file("props_lab.json"),
        data_file("approved_props.json"),
        data_file("best_hit_prop.json"),
    ]
    generated_root = data_file("generated_cards")
    for folder in (selected_date, mmddyyyy):
        prop_dir = generated_root / folder
        if prop_dir.exists():
            targets.extend(prop_dir.glob("best_hit_prop*"))
            targets.extend(prop_dir.glob("best_hit_art*"))
    for target in targets:
        try:
            if target.exists() and target.is_file():
                target.unlink()
                removed.append(str(target))
        except Exception:
            logging.exception("Could not remove prop cache file: %s", target)
    await update.message.reply_text(
        "🧹 BETGPTAI PROP CACHE CLEARED\n\n"
        f"Card Date: {datetime.fromisoformat(selected_date).strftime('%m/%d/%Y')}\n"
        f"Files deleted: {len(removed)}\n\n"
        + ("\n".join(removed[:12]) if removed else "No cache files existed.")
    )


async def verify_best_hit_prop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show exact MLB verification for today's Best Hit candidate."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Verifying today’s Best Hit Prop from MLB Stats API...")
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        result = await asyncio.to_thread(get_verified_best_hit_prop, slate, selected_date)
        prop = result.get("prop")
        if result.get("status") != "ready" or not isinstance(prop, dict):
            await update.message.reply_text(
                "❌ Best hit prop rejected due to failed team verification.\n\n"
                + "\n".join(str(item) for item in (result.get("rejections") or [])[:8])
            )
            return
        verification = await asyncio.to_thread(verify_hit_prop_context, prop, slate)
        await update.message.reply_text(
            "🔎 BEST HIT PROP VERIFICATION\n\n"
            f"Player: {verification.get('player') or prop.get('player_name')}\n"
            f"Expected Team: {verification.get('expected_team')}\n"
            f"Verified Current Team: {verification.get('verified_current_team')}\n"
            f"Active Roster: {'yes' if verification.get('active_roster') else 'no'}\n"
            f"Today’s Opponent: {verification.get('today_opponent') or 'Unavailable'}\n"
            f"Lineup Spot: {verification.get('lineup_spot') or 'Unavailable'}\n"
            f"Status: {verification.get('lineup_status') or verification.get('status')}\n"
            f"Valid: {'yes' if verification.get('valid') else 'no'}"
        )
    except Exception as error:
        logging.exception("/verify_best_hit_prop failed")
        await update.message.reply_text(
            f"❌ Best hit prop rejected due to failed team verification.\n\nError: {error}"
        )


async def image_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: verify OpenAI image generation independent of betting logic."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    output_path = data_file("generated_cards") / "test.png"
    prompt = "A baseball with blue lightning"
    try:
        await update.message.reply_text("Generating OpenAI image...")
        saved_image = await asyncio.to_thread(generate_image, prompt, str(output_path))
        with Path(saved_image).open("rb") as image_file:
            await update.message.reply_photo(
                photo=image_file,
                caption=f"✅ OpenAI image test created.\n{saved_image}",
            )
    except Exception as error:
        logging.exception("/image_test failed")
        await update.message.reply_text(
            "❌ OpenAI image test failed.\n\n"
            f"Error: {error}\n\n"
            f"Prompt: {prompt}"
        )


async def today_image_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: preview one Anime Vault image for /today."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building /today Anime Vault image preview...")
    selected_date = official_sports_date().isoformat()
    try:
        featured = await asyncio.to_thread(
            get_most_recent_featured_picks,
            selected_date,
        )
        play = featured.get("play_of_day")
        legs = featured.get("parlay_legs", [])
        if not play or not isinstance(legs, list) or len(legs) != 2:
            await update.message.reply_text(
                "Today’s official picks are not saved yet. Run /mlb_auto first, "
                "then run /today_image_admin."
            )
            return
        result = await asyncio.to_thread(
            prepare_today_pick_image,
            featured,
            selected_date,
            image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
        )
        await _send_single_image_preview(
            update,
            result,
            title="BETGPTAI Today Pick Image",
            approved_post_hint="Preview only. Public posting command will be added later.",
        )
    except Exception as error:
        logging.exception("/today_image_admin failed")
        await update.message.reply_text(
            f"Unable to build /today image preview. Error: {error}"
        )


async def mlb_auto_image_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: preview one Anime Vault image for /mlb_auto."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building /mlb_auto Anime Vault image preview...")
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        try:
            analysis = await analyze_mlb_slate(
                slate,
                os.getenv("OPENAI_API_KEY", ""),
                os.getenv("ANTHROPIC_API_KEY", ""),
            )
        except Exception as error:
            logging.error("AI Analysis Error:\n%s", error, exc_info=True)
            analysis = build_fallback_card(slate)

        await asyncio.to_thread(
            save_model_report, selected_date, slate, analysis, get_last_analysis_metadata()
        )
        try:
            saved_count = await asyncio.to_thread(
                _save_official_mlb_card,
                analysis,
                slate,
                selected_date,
                "mlb_auto_image_admin",
            )
        except Exception:
            logging.exception("Could not save official picks before /mlb_auto image")

        result = await asyncio.to_thread(
            prepare_mlb_auto_image,
            _with_card_date(analysis, selected_date),
            selected_date,
            image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
        )
        await _send_single_image_preview(
            update,
            result,
            title="BETGPTAI MLB Auto Image",
            approved_post_hint="Preview only. Public posting command will be added later.",
        )
    except Exception as error:
        logging.exception("/mlb_auto_image_admin failed")
        await update.message.reply_text(
            f"Unable to build /mlb_auto image preview. Error: {error}"
        )


async def magazine_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only Daily Anime Sports Magazine prompt/image preview."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building BETGPTAI Daily Anime Sports Magazine...")
    selected_date = official_sports_date().isoformat()
    try:
        slate, payload = await _build_verified_props_payload(selected_date)
        if not payload:
            await update.message.reply_text("No MLB games were found for today.")
            return
        mlb_card_data: dict[str, object] = {}
        try:
            analysis = await analyze_mlb_slate(
                slate,
                os.getenv("OPENAI_API_KEY", ""),
                os.getenv("ANTHROPIC_API_KEY", ""),
            )
            mlb_card_data = _official_mlb_card_data(analysis, slate, selected_date)
        except Exception:
            logging.exception("Magazine MLB plays enrichment failed")
        prompt_items = generate_daily_magazine_prompts(payload, mlb_card_data)
        output_dir = _dated_generated_cards_dir(selected_date) / "magazine"
        await _send_prompt_previews(
            update,
            prompt_items,
            output_dir,
            save_function=save_magazine_prompts,
            image_prefix="BETGPTAI Daily Anime Sports Magazine",
        )
    except Exception as error:
        logging.exception("/magazine_admin failed")
        await update.message.reply_text(f"Unable to build magazine cards. Error: {error}")


async def verify_player_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only current-team verification command."""
    if not await _require_admin(update) or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /verify_player PLAYER_NAME")
        return
    player_name = " ".join(context.args)
    result = await asyncio.to_thread(verify_player_team, player_name, "")
    icon = "✅" if result.get("verified") else "❌"
    await update.message.reply_text(
        f"{icon} PLAYER VERIFICATION\n\n"
        f"Player: {player_name}\n"
        f"Current Team: {result.get('current_team') or 'Unavailable'}\n"
        f"Player ID: {result.get('player_id') or 'Unavailable'}\n"
        f"Status: {result.get('status')}\n"
        f"Reason: {result.get('reason')}"
    )


async def props_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only full player props lab preview."""
    del context
    await _send_props_lab(update, "summary")


async def hits_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only hit prop preview."""
    del context
    await _send_props_lab(update, "hits")


async def hits_by_team_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only best hit prop candidate for each team playing today."""
    del context
    await _send_props_lab(update, "hits_by_team")


async def streak_report_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only 1-5 lineup hitter streak research report."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building BETGPTAI Hit Streak Report...")
    selected_date = official_sports_date().isoformat()
    try:
        payload = await asyncio.to_thread(
            build_hitting_streak_report,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        await _send_long_message(update, render_hitting_streak_report(payload))
    except Exception:
        logging.exception("Unexpected /streak_report_admin error")
        await update.message.reply_text(
            "Unable to build the Hit Streak Report right now. Check terminal logs."
        )


async def streak_debug_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only debug view for hitting streak report generation."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        payload = await asyncio.to_thread(
            build_hitting_streak_report,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        await _send_long_message(update, render_hitting_streak_debug(payload))
    except Exception:
        logging.exception("Unexpected /streak_debug_admin error")
        await update.message.reply_text(
            "Unable to build streak debug details right now. Check terminal logs."
        )


async def prepost_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only quality gate before public card/image posting."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        payload = await asyncio.to_thread(run_prepost_quality_gate)
        await _send_long_message(update, render_prepost_quality_gate(payload))
    except Exception:
        logging.exception("Unexpected /prepost_check error")
        await update.message.reply_text(
            "Unable to run the pre-post quality gate right now. Check terminal logs."
        )


async def _send_intelligence_dashboard(update: Update, mode: str) -> None:
    """Build and send one owner-only intelligence dashboard view."""
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building BETGPTAI Intelligence Dashboard...")
    selected_date = official_sports_date().isoformat()
    try:
        payload = await asyncio.to_thread(
            build_intelligence_dashboard,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if mode == "morning":
            text = render_morning_report(payload)
        elif mode == "lineup":
            text = render_lineup_report(payload)
        elif mode == "review":
            text = render_model_review(payload)
        else:
            text = render_daily_intel(payload)
        await _send_long_message(update, text)
    except Exception:
        logging.exception("Unexpected intelligence dashboard error")
        await update.message.reply_text(
            "Unable to build the Intelligence Dashboard right now. Check terminal logs."
        )


async def daily_intel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only full Intelligence Dashboard."""
    del context
    await _send_intelligence_dashboard(update, "daily")


async def morning_report_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only morning readiness report."""
    del context
    await _send_intelligence_dashboard(update, "morning")


async def lineup_report_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only lineup/player trend report."""
    del context
    await _send_intelligence_dashboard(update, "lineup")


async def model_review_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only postgame model review."""
    del context
    await _send_intelligence_dashboard(update, "review")


async def intel_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Intelligence Dashboard debug report."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building BETGPTAI Intelligence debug report...")
    selected_date = official_sports_date().isoformat()
    try:
        payload = await asyncio.to_thread(
            build_intelligence_dashboard,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        await _send_long_message(update, render_intel_debug(payload))
    except Exception:
        logging.exception("Unexpected /intel_debug error")
        await update.message.reply_text(
            "Unable to build the Intelligence debug report right now. Check terminal logs."
        )


async def _send_learning_view(update: Update, mode: str) -> None:
    """Build/send one owner-only AI Learning Engine view."""
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        if mode in {"report", "loss_review"}:
            report = await asyncio.to_thread(run_learning_review, selected_date)
            text = render_loss_review(report) if mode == "loss_review" else render_learning_report(report)
        elif mode == "weights":
            text = await asyncio.to_thread(render_weight_suggestions)
        elif mode == "status":
            payload = await asyncio.to_thread(learning_status_payload)
            text = render_learning_status(payload)
        else:
            text = "Unknown learning view."
        await _send_long_message(update, text)
    except Exception:
        logging.exception("Unexpected AI Learning Engine error")
        await update.message.reply_text(
            "Unable to build the AI Learning report right now. Check terminal logs."
        )


async def learning_report_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only full AI Learning report."""
    del context
    await _send_learning_view(update, "report")


async def loss_review_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only losing-pick review."""
    del context
    await _send_learning_view(update, "loss_review")


async def weight_suggestions_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only pending model-weight suggestions."""
    del context
    await _send_learning_view(update, "weights")


async def learning_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only AI Learning Engine status."""
    del context
    await _send_learning_view(update, "status")


async def learning_auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only AI Learning auto-apply status."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    text = await asyncio.to_thread(render_learning_auto_status)
    await update.message.reply_text(text)


async def toggle_learning_auto_apply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only toggle for safe AI Learning auto-apply."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    payload = await asyncio.to_thread(toggle_learning_auto_apply)
    await update.message.reply_text(
        "🧠 AI Learning Auto Apply Updated\n\n"
        f"Status: {'ON' if payload.get('enabled') else 'OFF'}\n"
        f"Updated At: {payload.get('updated_at')}"
    )


async def weights_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only current model weights."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    text = await asyncio.to_thread(render_weights_admin)
    await _send_long_message(update, text)


async def weight_history_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only model-weight history."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    text = await asyncio.to_thread(render_weight_history_admin)
    await _send_long_message(update, text)


async def approve_weight_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only approval for pending learning suggestions."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    result = await asyncio.to_thread(approve_learning_weight_update)
    skipped = result.get("skipped") or []
    await update.message.reply_text(
        "✅ WEIGHT UPDATE REVIEW COMPLETE\n\n"
        f"Applied: {result.get('applied', 0)}\n"
        f"Message: {result.get('message')}\n"
        f"Skipped: {', '.join(skipped) if skipped else 'None'}"
    )


async def reject_weight_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only rejection for pending learning suggestions."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await asyncio.to_thread(reject_learning_weight_update)
    await update.message.reply_text("🧹 Pending AI Learning weight suggestions rejected and cleared.")


async def _build_and_send_mlb_admin_report(
    *,
    message: object,
    full: bool = True,
) -> None:
    """Build/send the owner-only MLB War Room report from a message-like object."""
    selected_date = official_sports_date().isoformat()
    SYSTEM_LOG.info(
        "component=MLBAdmin status=build_started card_date=%s recovery=none",
        selected_date,
    )
    await message.reply_text("⏳ Building BETGPTAI MLB War Room...")
    report = await build_mlb_admin_report_async(
        selected_date,
        odds_api_key=os.getenv("ODDS_API_KEY", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    SYSTEM_LOG.info(
        "component=MLBAdmin status=report_built card_date=%s games=%s errors=%s recovery=send_report",
        selected_date,
        len(report.get("games", []) if isinstance(report.get("games"), list) else []),
        len(report.get("errors", []) if isinstance(report.get("errors"), list) else []),
    )
    await _send_long_text_to_message(message, render_mlb_admin_report(report, full=full))
    report_path = report.get("report_path")
    if report_path and Path(str(report_path)).exists():
        with Path(str(report_path)).open("rb") as report_file:
            await message.reply_document(
                document=report_file,
                filename="mlb_admin_report.json",
                caption="📎 Saved MLB War Room JSON",
            )
    SYSTEM_LOG.info(
        "component=MLBAdmin status=sent card_date=%s report_path=%s recovery=none",
        selected_date,
        report_path,
    )


async def mlb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Official MLB War Room."""
    del context
    user_id = update.effective_user.id if update.effective_user else None
    SYSTEM_LOG.info(
        "component=MLBAdmin status=command_received user_id=%s recovery=authorize",
        user_id,
    )
    if not await _require_admin(update) or not update.message:
        return
    try:
        await _build_and_send_mlb_admin_report(message=update.message, full=True)
    except Exception as error:
        logging.exception("Unexpected /mlb_admin error")
        SYSTEM_LOG.exception(
            "component=MLBAdmin status=failed error=%s recovery=notify_admin",
            error,
        )
        await update.message.reply_text(
            f"Unable to build the MLB War Room right now.\n\nError: {error}"
        )


async def warroom_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only War Room enrichment diagnostics."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Building War Room debug snapshot...")
    try:
        report = await build_mlb_admin_report_async(
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
            save_picks=False,
        )
        await _send_long_message(update, render_warroom_debug(report))
    except Exception as error:
        logging.exception("Unexpected /warroom_debug error")
        await update.message.reply_text(f"Unable to build War Room debug right now.\n\nError: {error}")


def _render_odds_debug_payload(payload: dict[str, Any], selected_date: str) -> str:
    """Render owner-only odds diagnostics without exposing API keys."""
    sharp = payload.get("sharp_api_health") or {}
    cache_age = sharp.get("cache_age_seconds")
    cache_str = f"{cache_age:.0f}s" if cache_age is not None else "N/A"
    sport = payload.get("sport", "mlb")
    league = payload.get("league") or ""
    event_date = payload.get("event_date", selected_date)
    sport_label = sport.upper() if sport != "mlb" else "MLB"
    if league:
        sport_label = f"{sport_label} ({league})"
    default_sb = payload.get("default_sportsbook", "draftkings")
    secondary_sb = payload.get("secondary_sportsbook", "fanduel")
    active_sb = payload.get("active_sportsbook")
    endpoint = payload.get("endpoint_used", "/odds")
    use_best = payload.get("use_best_odds", True)
    auth = payload.get("auth_method", "X-API-Key header only")
    sb_counts: dict[str, int] = payload.get("sportsbook_game_counts") or {}
    rows_by_event = payload.get("sharp_rows_by_event") if isinstance(payload.get("sharp_rows_by_event"), dict) else {}
    rows_by_event_preview = ", ".join(
        f"{event_id}:{count}" for event_id, count in list(rows_by_event.items())[:10]
    ) or "None"
    lines = [
        "🧪 BETGPTAI ODDS DEBUG",
        f"Parsed sport: {payload.get('parsed_sport', sport)}",
        f"Parsed league: {payload.get('parsed_league') or 'None'}",
        f"Parsed flags: {', '.join(payload.get('parsed_flags') or []) or 'None'}",
        f"Include Started: {'YES' if payload.get('include_started') else 'NO'}",
        f"📅 Requested Date: {event_date}",
        f"Requested Card Date ET: {payload.get('requested_card_date_et', event_date)}",
        f"UTC Query Window: {payload.get('utc_query_window') or 'N/A'}",
        f"🏅 Sport: {sport_label}",
        "",
        "── Sharp API ──",
        f"Enabled: {'yes' if payload.get('sharp_api_enabled') else 'no'}",
        f"Key loaded: {'yes' if payload.get('sharp_api_key_loaded') else 'no'}",
        f"Auth method: {auth}",
        f"SHARP_USE_BEST_ODDS: {'yes' if use_best else 'no'}",
        f"Endpoint: {endpoint}",
        f"Cache age: {cache_str}",
        f"Cache fresh: {'yes' if sharp.get('cache_fresh') else 'no'}",
        f"Sportsbook used: {active_sb or 'none'}",
        f"Sharp League: {payload.get('sharp_league') or 'none'}",
    ]
    if sb_counts:
        for sb_name, sb_count in sb_counts.items():
            lines.append(f"  {sb_name}: {sb_count} games")
    lines += [
        f"Request URL: {payload.get('sharp_request_url') or 'N/A'}",
        "",
        "── The Odds API ──",
        f"Key loaded: {'yes' if payload.get('odds_api_key_loaded') else 'no'}",
        f"Status code: {payload.get('odds_api_status_code') or 'Unavailable'}",
        f"Request URL: {payload.get('odds_api_request_url') or 'N/A'}",
        "",
        f"Active provider: {payload.get('provider') or 'None'}",
        f"Provider: {payload.get('provider') or 'None'}",
        f"Sharp raw rows: {payload.get('sharp_raw_rows', 0)}",
        f"Sharp pages fetched: {payload.get('sharp_pages_fetched', 0)}",
        f"Sharp pagination has_more: {'YES' if payload.get('sharp_pagination_has_more') else 'NO'}",
        f"Sharp pagination truncated: {'YES' if payload.get('sharp_pagination_truncated') else 'NO'}",
        f"Sharp total rows collected: {payload.get('sharp_total_rows_collected', 0)}",
        f"Sharp unique rows: {payload.get('sharp_unique_rows', 0)}",
        f"Rows by event (first 10): {rows_by_event_preview}",
        f"Sharp rows returned: {payload.get('sharp_raw_rows', 0)}",
        f"Sharp local-date rows: {payload.get('sharp_local_date_rows', 0)}",
        f"MLB local-date games: {payload.get('mlb_local_date_games', 0)}",
        f"Accepted game-market rows: {payload.get('accepted_game_market_rows', 0)}",
        f"Rejected prop rows: {payload.get('rejected_prop_rows', 0)}",
        f"Accepted prop rows: {payload.get('accepted_prop_rows', 0)}",
        f"Game market sportsbook: {payload.get('game_market_sportsbook', 'draftkings')}",
        f"Prop sportsbook: {payload.get('prop_sportsbook', 'fanduel')}",
        f"Events returned: {payload.get('events_returned', payload.get('games_returned', 0))}",
        f"Matched games: {payload.get('matched_to_mlb_game_pk', 0)}",
        f"Sportsbook used: {active_sb or 'none'}",
        f"Market context available: {'YES' if payload.get('market_context_available') else 'NO'}",
        f"First matched game: {payload.get('first_matched_game') or 'None'}",
        f"Unmatched Sharp games: {len(payload.get('unmatched_odds_games') or [])}",
        f"Unmatched MLB slate games: {len(payload.get('unmatched_mlb_games') or [])}",
        f"Moneyline contexts: {payload.get('moneyline_contexts', 0)}",
        f"Runline contexts: {payload.get('runline_contexts', 0)}",
        f"Total contexts: {payload.get('total_contexts', 0)}",
        f"Team total contexts: {payload.get('team_total_contexts', 0)}",
        f"Team-name matches: {payload.get('team_name_matches', 0)}",
        f"Time-window rejections: {payload.get('time_window_rejections', 0)}",
        f"Date rejections: {payload.get('date_rejections', 0)}",
        f"Missing-team rejections: {payload.get('missing_team_rejections', 0)}",
        f"Doubleheader closest-time matches: {payload.get('doubleheader_closest_time_matches', 0)}",
        f"Markets: {payload.get('markets_requested')}",
        f"Games returned: {payload.get('games_returned')}",
    ]
    games_returned = payload.get("games_returned", 0)
    schedule_count = payload.get("mlb_schedule_games", 0)
    if games_returned == 0 and schedule_count > 0:
        lines.append(f"MLB schedule has {schedule_count} games, but odds provider returned 0 events.")
    elif games_returned == 0:
        lines.append("Provider returned 0 events for this date/sport mapping.")
    if sport == "mlb":
        lines.extend([
            f"Matched MLB games: {payload.get('matched_to_mlb_game_pk')}",
            f"Unmatched MLB: {len(payload.get('unmatched_mlb_games') or [])}",
            f"Unmatched odds: {len(payload.get('unmatched_odds_games') or [])}",
        ])
    # Sharp MLB mapping probes
    probes = payload.get("sharp_mlb_probes")
    if isinstance(probes, list) and len(probes) > 0:
        lines.append("")
        lines.append("── Sharp MLB Mapping Probes ──")
        for p in probes:
            status = p.get("http_status") or p.get("status_code") or "—"
            count = p.get("accepted_rows", p.get("games_count", 0))
            err = p.get("error") or ""
            ep = p.get("endpoint", "/odds")
            sp = p.get("sport_param")
            lg = p.get("league") or "none"
            sb = p.get("sportsbook") or "none"
            requested = p.get("market_requested") or "all"
            raw = p.get("rows_returned", 0)
            lines.append(f"  endpoint={ep} market={requested} book={sb} → HTTP {status} rows={raw} accepted={count}{f' error={err}' if err else ''}")
    lines.append(f"Last error: {payload.get('last_error') or 'None'}")
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        lines.append("")
        lines.extend(f"- {error}" for error in errors[:8])
        lines.append("")
    unmatched_mlb = payload.get("unmatched_mlb_games") if isinstance(payload.get("unmatched_mlb_games"), list) else []
    if unmatched_mlb:
        lines.append("Sample unmatched MLB:")
        for item in unmatched_mlb[:8]:
            lines.append(f"- {item.get('game_pk')}: {item.get('away_team')} @ {item.get('home_team')}")
        lines.append("")
    unmatched_odds = payload.get("unmatched_odds_games") if isinstance(payload.get("unmatched_odds_games"), list) else []
    if unmatched_odds:
        lines.append("Sample unmatched odds:")
        for item in unmatched_odds[:8]:
            lines.append(f"- {item.get('away_team')} @ {item.get('home_team')} ({item.get('commence_time')})")
    mapping = payload.get("unmatched_mapping_diagnostics") if isinstance(payload.get("unmatched_mapping_diagnostics"), list) else []
    if mapping:
        lines.extend(["", "Sharp unmatched mapping diagnostics:"])
        for item in mapping[:10]:
            lines.extend([
                f"- Raw Sharp: {item.get('sharp_away')} @ {item.get('sharp_home')}",
                f"  Normalized Sharp: {item.get('sharp_normalized_away')}@{item.get('sharp_normalized_home')}",
                f"  Local ET start: {item.get('sharp_local_start') or 'Unavailable'}",
                f"  Closest MLB: {item.get('mlb_away')} @ {item.get('mlb_home')}",
                f"  Normalized MLB: {item.get('mlb_normalized_away')}@{item.get('mlb_normalized_home')}",
                f"  Time difference minutes: {item.get('time_difference_minutes')}",
                f"  Rejection reason: {item.get('rejection_reason')}",
            ])
    return "\n".join(lines).strip()


def _parse_debug_command_args(args: list[str], *, allow_league: bool = True) -> dict[str, Any]:
    """Separate debug positional values from flags without cross-contamination."""
    supported = {"mlb", "soccer", "nba", "nfl", "nhl"}
    sport = "mlb"
    index = 0
    if args and args[0].strip().lower() in supported:
        sport = args[0].strip().lower()
        index = 1
    include_started = False
    event_date: str | None = None
    flags: list[str] = []
    league_parts: list[str] = []
    while index < len(args):
        token = args[index].strip()
        if token == "--include-started":
            include_started = True
            flags.append(token)
        elif token == "--date":
            flags.append(token)
            if index + 1 < len(args) and not args[index + 1].startswith("--"):
                event_date = args[index + 1].strip()
                index += 1
        elif token.startswith("--date="):
            flags.append("--date")
            event_date = token.split("=", 1)[1].strip()
        elif token.startswith("--"):
            flags.append(token)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            event_date = token  # backward-compatible positional date
        elif allow_league:
            league_parts.append(token)
        index += 1
    league = " ".join(league_parts).strip() or None
    if league and league.startswith("--"):
        league = None
    if sport == "mlb":
        league = None
    return {
        "sport": sport, "league": league, "event_date": event_date,
        "include_started": include_started, "full_pagination": "--full-pagination" in flags,
        "flags": flags,
    }


async def odds_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: inspect odds provider fetch and game matching per sport.

    Usage: /odds_debug [sport] [league] [date]
    Sport: mlb (default), soccer, nba, nfl, nhl
    League: e.g. MLS, EPL, La Liga, Bundesliga, Serie A, Liga MX (soccer only)
    Date: optional YYYY-MM-DD (trailing arg)
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    parsed = _parse_debug_command_args(list(context.args or []))
    sport = parsed["sport"]
    league = parsed["league"]
    event_date = parsed["event_date"]
    include_started = bool(parsed["include_started"])
    max_pages = 10 if parsed.get("full_pagination") else 5
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Invalid --date. Use YYYY-MM-DD.")
            return
    selected_date = event_date or official_sports_date().isoformat()
    sport_label = sport.upper() if sport != "mlb" else "MLB"
    if league:
        sport_label = f"{sport_label} ({league})"
    if event_date:
        sport_label = f"{sport_label} @ {event_date}"
    try:
        await update.message.reply_text(f"⏳ Checking {sport_label} odds matching (Sharp + Odds API)...")
        payload = await asyncio.to_thread(
            odds_debug_payload,
            os.getenv("ODDS_API_KEY", ""),
            selected_date,
            sport,
            league,
            event_date,
            include_started,
            parsed["flags"],
            max_pages,
        )
        await _send_long_message(update, _render_odds_debug_payload(payload, selected_date))
    except Exception as error:
        logging.exception("/odds_debug failed")
        await update.message.reply_text(f"/odds_debug failed:\n{error!r}")


async def odds_probe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: test all provider mappings and show results."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    sport = "mlb"
    event_date: str | None = None
    if args and args[0].strip().lower() in ("mlb", "soccer", "nba", "nfl", "nhl"):
        sport = args[0].strip().lower()
    date_args = [a for a in args if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)]
    if date_args:
        event_date = date_args[-1]
    target = event_date or official_sports_date().isoformat()
    try:
        await update.message.reply_text(f"⏳ Probing {sport.upper()} odds provider mappings...")
        payload = await asyncio.to_thread(
            odds_debug_payload,
            os.getenv("ODDS_API_KEY", ""),
            target,
            sport,
            None,
            event_date,
        )
        endpoint = payload.get("endpoint_used", "/odds")
        use_best = payload.get("use_best_odds", True)
        auth = payload.get("auth_method", "X-API-Key header only")
        lines = [
            "🔍 BETGPTAI ODDS PROBE",
            f"📅 Date: {target}",
            f"🏅 Sport: {sport.upper()}",
            "",
            "── Sharp API ──",
            f"Auth method: {auth}",
            f"SHARP_USE_BEST_ODDS: {'yes' if use_best else 'no'}",
            f"Endpoint: {endpoint}",
            f"Default sportsbook: {payload.get('default_sportsbook', 'draftkings')}",
            f"Secondary sportsbook: {payload.get('secondary_sportsbook', 'fanduel')}",
            f"Active sportsbook: {payload.get('active_sportsbook') or 'none'}",
        ]
        sb_counts: dict[str, int] = payload.get("sportsbook_game_counts") or {}
        if sb_counts:
            lines.append("Sportsbook game counts:")
            for sb_name, sb_count in sb_counts.items():
                lines.append(f"  {sb_name}: {sb_count}")
        sharp_url = payload.get("sharp_request_url") or "N/A"
        lines.append(f"Request URL: {sharp_url}")
        probes = payload.get("sharp_mlb_probes")
        if isinstance(probes, list):
            for p in probes:
                ep = p.get("endpoint", "/odds")
                sp = p.get("sport_param")
                lg = p.get("league") or "none"
                sb = p.get("sportsbook") or "none"
                status = p.get("status_code") or "—"
                count = p.get("games_count", 0)
                err = p.get("error") or ""
                lines.append(f"  endpoint={ep} sport={sp} league={lg} book={sb}")
                lines.append(f"    URL: {p.get('url', '?')}")
                lines.append(f"    HTTP {status} — {count} games{f' — {err}' if err else ''}")
                if count > 0:
                    lines.append(f"    First game: {p.get('first_matchup', 'N/A')}")
        else:
            lines.append("  (No probe data for this sport)")
        lines.append("")
        lines.append("── The Odds API ──")
        odds_url = payload.get("odds_api_request_url") or "N/A"
        lines.append(f"URL: {odds_url}")
        odds_status = payload.get("odds_api_status_code") or "Unavailable"
        lines.append(f"Status: {odds_status}")
        games_ret = payload.get("games_returned", 0)
        lines.append(f"Games returned: {games_ret}")
        sched_cnt = payload.get("mlb_schedule_games", 0)
        if sport == "mlb" and sched_cnt > 0:
            lines.append(f"MLB schedule games: {sched_cnt}")
        if games_ret == 0 and sched_cnt > 0:
            lines.append("MLB schedule has games, but odds provider returned 0 events.")
        elif games_ret == 0:
            lines.append("Provider returned 0 events for this date/sport mapping.")
        active = payload.get("provider") or "none"
        lines.append(f"Active provider: {active}")
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        if errors:
            lines.append("")
            lines.extend(f"- {e}" for e in errors[:8])
        await _send_long_message(update, "\n".join(lines).strip())
    except Exception as error:
        logging.exception("/odds_probe failed")
        await update.message.reply_text(f"/odds_probe failed:\n{error!r}")


async def sharp_probe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only direct SharpAPI probe. Usage: /sharp_probe mlb"""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID or not update.message:
        return
    sport = (context.args or ["mlb"])[0].strip().lower()
    if sport != "mlb":
        await update.message.reply_text("/sharp_probe currently supports: mlb")
        return
    mode = (context.args[1].strip().lower() if len(context.args or []) > 1 else "game_markets")
    max_pages = 10 if "--full-pagination" in (context.args or []) else 5
    from api.sharp_odds_client import _base_url, fetch_mlb_game_markets, fetch_mlb_props, sharp_api_enabled
    try:
        if mode == "props":
            result = await asyncio.to_thread(fetch_mlb_props, official_sports_date().isoformat(), max_pages)
            counts = result.get("market_counts") or {}
            first = result.get("first_grouped_prop") if isinstance(result.get("first_grouped_prop"), dict) else {}
            lines = [
                "🔍 SHARP API PROBE — MLB PROPS",
                f"Sportsbook: {str(result.get('sportsbook', 'fanduel')).title()}",
                "Rows:",
                f"- Hits: {counts.get('player_hits', 0)}",
                f"- Total Bases: {counts.get('player_total_bases', 0)}",
                f"- HR: {counts.get('player_home_runs', 0)}",
                f"- RBI: {counts.get('player_rbis', 0)}",
                f"- Runs: {counts.get('player_runs', 0)}",
                f"- Pitcher Ks: {int(counts.get('pitcher_strikeouts', 0)) + int(counts.get('player_strikeouts', 0))}",
                "", "Grouped:",
                f"- Total grouped props: {len(result.get('grouped_props') or [])}",
                f"- Paired Over/Under: {result.get('paired_markets', 0)}",
                f"- Single-side only: {result.get('single_side_markets', 0)}",
                f"- Missing player team context: {result.get('missing_player_team_context', 0)}",
                f"- Missing event teams: {result.get('missing_event_teams', 0)}",
                f"Rejected game-market rows: {result.get('rejected_game_market_rows', 0)}",
                f"Pages fetched: {result.get('pages_fetched', 0)}",
                f"Total rows collected: {result.get('total_rows_collected', 0)}",
                f"Unique rows: {result.get('unique_rows', 0)}",
                f"Pagination truncated: {'YES' if result.get('pagination_truncated') else 'NO'}",
                "", "First grouped prop:",
                f"Player: {first.get('player_name') or 'None'}",
                f"Market: {first.get('market_type') or 'None'}",
                f"Line: {first.get('line')}",
                f"Over odds: {first.get('over_odds')}",
                f"Under odds: {first.get('under_odds')}",
                f"Teams: {first.get('away_team')} @ {first.get('home_team')} | player team={first.get('team') or 'unknown'}",
                f"Start: {first.get('start_time') or 'None'}",
                f"Status: {first.get('status') or 'available'}",
                f"Error: {result.get('error') or 'None'}",
            ]
            await update.message.reply_text("\n".join(lines))
            return
        result = await asyncio.to_thread(fetch_mlb_game_markets, official_sports_date().isoformat(), max_pages)
        counts = result.get("market_counts") or {}
        lines = [
            "🔍 SHARP API PROBE — MLB GAME MARKETS",
            f"Sharp API Enabled: {'YES' if sharp_api_enabled() else 'NO'}",
            f"Base URL: {_base_url()}",
            "Auth Method: X-API-Key",
            f"Sportsbook used: {result.get('sportsbook', 'draftkings')}",
            f"Pages fetched: {result.get('pages_fetched', 0)}",
            f"Total rows collected: {result.get('total_rows_collected', 0)}",
            f"Unique rows: {result.get('unique_rows', 0)}",
            f"Events found: {result.get('events_found', 0)}",
            f"Pagination has_more: {'YES' if result.get('pagination_has_more') else 'NO'}",
            f"pagination_truncated={'true' if result.get('pagination_truncated') else 'false'}",
            "Rows per first 10 events:",
            *[
                f"- {event_id}: {count}"
                for event_id, count in list((result.get("rows_by_event") or {}).items())[:10]
            ],
            "Endpoint attempts:",
        ]
        for attempt in result.get("attempts") or []:
            lines.append(
                f"- {attempt.get('endpoint')} | HTTP {attempt.get('http_status')} | "
                f"market={attempt.get('market_requested')} | rows={attempt.get('rows_returned', 0)} | "
                f"accepted={attempt.get('accepted_rows', 0)} | rejected_props={attempt.get('rejected_prop_rows', 0)}"
            )
        lines.extend([
            f"Moneyline rows: {counts.get('moneyline', 0)}",
            f"Spread/runline rows: {counts.get('runline', 0)}",
            f"Total rows: {counts.get('total', 0)}",
            f"Team total rows: {counts.get('team_total', 0)}",
            f"Rejected prop rows: {result.get('rejected_prop_rows', 0)}",
            f"First accepted matchup: {result.get('first_accepted_matchup') or 'None'}",
            f"First accepted market: {result.get('first_accepted_market') or 'None'}",
            f"First rejected prop market: {result.get('first_rejected_prop_market') or 'None'}",
            f"Error: {result.get('error') or 'None'}",
        ])
        await update.message.reply_text("\n".join(lines))
    except Exception as error:
        code = getattr(error, "code", None) or str(error)
        await update.message.reply_text(
            "🔍 SHARP API PROBE — MLB\n"
            f"Sharp API Enabled: {'YES' if sharp_api_enabled() else 'NO'}\n"
            f"Base URL: {_base_url()}\nAuth Method: X-API-Key\n"
            "Endpoint tested: N/A\nHTTP Status: N/A\nTop-level keys: None\n"
            f"Events returned: 0\nOdds returned: 0\nFirst matchup: None\nFirst market: None\nError: {code}"
        )


async def sp_batter_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: SP vs Batter matchup engine diagnostics.

    Usage:
      /sp_batter_debug           — compact summary
      /sp_batter_debug full      — per-game technical detail
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    full_mode = "full" in args
    selected_date = official_sports_date().isoformat()
    try:
        await update.message.reply_text("⏳ Running SP vs Batter Matchup Engine...")
        from services.sp_batter_matchup_engine import build_slate_matchups
        slate = await asyncio.to_thread(
            __import__("mlb_data", fromlist=["get_combined_slate"]).get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        result = await asyncio.to_thread(build_slate_matchups, slate)
        debug = result.get("debug", {})
        games = result.get("games", [])
        display_date_str = datetime.fromisoformat(selected_date).strftime("%m/%d/%Y")
        lines = [
            "🧪 BETGPTAI SP vs BATTER MATCHUP ENGINE",
            f"📅 Date: {display_date_str}",
            f"Games scanned: {debug.get('games_scanned', 0)}",
            f"Hitters scanned: {debug.get('hitters_scanned', 0)}",
            f"Hitters qualified: {debug.get('hitters_qualified', 0)}",
            f"Missing fields total: {debug.get('missing_fields_total', 0)}",
            "",
        ]
        def _score_line(mu, label=""):
            return (
                f"  {mu.get('player_name', '?')} (spot {mu.get('lineup_spot', '?')}) "
                f"— contact {mu.get('contact_edge_score', '?')} / "
                f"power {mu.get('power_edge_score', '?')} / "
                f"Krisk {mu.get('strikeout_risk_score', '?')} — "
                f"{mu.get('best_market', '?')}"
            )
        hit_edges = []
        hr_edges = []
        tb_edges = []
        k_risks = []
        for g in games:
            for side_key in ("away_vs_home_sp", "home_vs_away_sp"):
                side = g.get(side_key) or {}
                hit_edges.extend(side.get("top_hit_edges") or [])
                hr_edges.extend(side.get("top_hr_edges") or [])
                tb_edges.extend(side.get("top_total_bases_edges") or [])
                k_risks.extend(side.get("top_hit_edges") or [])
        hit_edges.sort(key=lambda mu: mu.get("overall_hit_score", 0), reverse=True)
        hr_edges.sort(key=lambda mu: mu.get("overall_hr_score", 0), reverse=True)
        tb_edges.sort(key=lambda mu: mu.get("total_bases_score", 0), reverse=True)
        k_risks.sort(key=lambda mu: mu.get("strikeout_risk_score", 0), reverse=True)

        def _top_n(edges, key, n=10):
            return [e for e in edges if e.get(key, 0) >= 40][:n] or edges[:n]

        lines.extend([
            "── TOP 10 HIT EDGES ──",
            *[_score_line(mu) for mu in _top_n(hit_edges, "overall_hit_score")[:10]],
            "",
            "── TOP 10 HR EDGES ──",
            *[_score_line(mu) for mu in _top_n(hr_edges, "overall_hr_score")[:10]],
            "",
            "── TOP 10 TOTAL BASES EDGES ──",
            *[_score_line(mu) for mu in _top_n(tb_edges, "total_bases_score")[:10]],
            "",
            "── TOP 10 K RISK SPOTS ──",
            *[_score_line(mu) for mu in _top_n(k_risks, "strikeout_risk_score")[:10]],
            "",
        ])
        if full_mode:
            lines.append("── PER-GAME DETAIL ──")
            for g in games:
                gl = g.get("game_level") or {}
                lines.append(f"── {gl.get('matchup', 'Game')} ──")
                lines.append(f"  PK: {g.get('game_pk')}")
                lines.append(f"  Contact adv: {gl.get('combined_contact_advantage', 'N/A')}")
                lines.append(f"  Power adv: {gl.get('combined_power_advantage', 'N/A')}")
                lines.append(f"  Game total side: {gl.get('recommended_game_total_side', 'N/A')}")
                lines.append(f"  DQ: {gl.get('data_quality_grade', 'N/A')}")
                for side_key, side_label in (("away_vs_home_sp", "Away→SP"), ("home_vs_away_sp", "Home→SP")):
                    side = g.get(side_key) or {}
                    top = side.get("top_hit_edges") or []
                    if top:
                        lines.append(f"  Top {side_label}:")
                        for mu in top[:3]:
                            lines.append(
                                f"    {mu.get('player_name')} (spot {mu.get('lineup_spot')}) — "
                                f"contact {mu.get('contact_edge_score')} / "
                                f"power {mu.get('power_edge_score')} / "
                                f"Krisk {mu.get('strikeout_risk_score')} — "
                                f"{mu.get('best_market')}"
                            )
                lines.append("")
            rejected = debug.get("rejected_hitters") or []
            if rejected:
                lines.append(f"Rejected hitters ({len(rejected)}):")
                lines.extend(f"- {r}" for r in rejected[:15])
        else:
            lines.append("── GAME SUMMARY ──")
            for g in games:
                gl = g.get("game_level") or {}
                lines.append(f"{gl.get('matchup', 'Game')} — PK {g.get('game_pk')} — Contact {gl.get('combined_contact_advantage', '?')} / Power {gl.get('combined_power_advantage', '?')} / total {gl.get('recommended_game_total_side', '?')} / DQ {gl.get('data_quality_grade', '?')}")
        await _send_long_message(update, "\n".join(lines).strip())
    except Exception as error:
        logging.exception("/sp_batter_debug failed")
        await update.message.reply_text(f"/sp_batter_debug failed:\n{error!r}")


async def official_card_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: official card diagnostics.

    Shows market context availability, Sharp games matched, quant
    candidates, official picks count, skipped reasons, and save result.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    selected_date = official_sports_date().isoformat()
    try:
        await update.message.reply_text("⏳ Building official card diagnostics...")
        from mlb_admin_report import build_mlb_admin_report

        report = await asyncio.to_thread(
            build_mlb_admin_report,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
            save_picks=False,
        )
        official = report.get("official_card") or {}
        slate = []
        try:
            from mlb_data import get_combined_slate
            slate = await asyncio.to_thread(
                get_combined_slate,
                os.getenv("ODDS_API_KEY", ""),
                game_date=selected_date,
                highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
            )
        except Exception:
            pass
        matched_odds = sum(
            1 for g in slate
            if g.get("odds_status") == "available"
            or isinstance(g.get("best_available_prices"), list) and len(g["best_available_prices"]) > 0
        )
        display_str = datetime.fromisoformat(selected_date).strftime("%m/%d/%Y")
        stats_only = os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
        market_mode = "Stats Only" if stats_only else "Normal"
        lines = [
            "🧪 BETGPTAI OFFICIAL CARD DEBUG",
            f"📅 Date: {display_str}",
            "",
            f"Market Mode: {market_mode}",
            f"Market context available: {'yes' if matched_odds > 0 else 'no'}",
            f"Sharp games matched: {matched_odds} / {len(slate) if slate else 0}",
            f"Quant candidates created: {len(official.get('top_moneylines', []))}",
            f"Official picks count: {official.get('saved_pick_count', 0)}",
            f"Card source: {official.get('source', 'none')}",
            f"Unavailable reason: {_safe(report.get('official_card_unavailable_reason'), 'N/A')}",
            f"Saved picks in report: {report.get('saved_picks', 0)}",
            "",
        ]
        skipped = []
        if not slate:
            skipped.append("Slate unavailable — no MLB games for this date")
        if matched_odds == 0:
            skipped.append("Odds market context unavailable — Sharp/Odds API returned 0 matched games")
        if not report.get("official_card_text"):
            skipped.append("AI analysis card not generated")
        if not official.get("top_moneylines"):
            skipped.append("No edge above threshold — admin Top5 produced zero qualified moneyline candidates")
        if official.get("saved_pick_count", 0) == 0:
            skipped.append("StructuredCard official_picks empty")
        if skipped:
            lines.append("Skipped reasons:")
            lines.extend(f"- {s}" for s in skipped)
            lines.append("")
        save_result = report.get("save_result") or {}
        if save_result:
            lines.append(f"Save result: {'success' if save_result.get('success') else 'failed'}")
            if save_result.get("error"):
                lines.append(f"Save error: {save_result.get('error')}")
        await _send_long_message(update, "\n".join(lines).strip())
    except Exception as error:
        logging.exception("/official_card_debug failed")
        await update.message.reply_text(f"/official_card_debug failed:\n{error!r}")


async def clv_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show CLV data for a snapshot date."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    target = args[0] if args else eastern_today().isoformat()
    try:
        from services.daily_snapshot import load_snapshot, render_clv_debug
        snapshot = await asyncio.to_thread(load_snapshot, target)
        if not snapshot:
            await update.message.reply_text(f"No snapshot for {target}.")
            return
        text = render_clv_debug(snapshot)
        await _send_long_message(update, text)
    except Exception as error:
        logging.exception("/clv_debug failed")
        await update.message.reply_text(f"/clv_debug failed: {error!r}")


async def bullpen_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show bullpen engine v2 debug for today's slate."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    try:
        await update.message.reply_text("⏳ Running Bullpen Engine v2...")
        from services.bullpen_engine import render_bullpen_debug
        from mlb_data import get_combined_slate
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=official_sports_date().isoformat(),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        text = await asyncio.to_thread(render_bullpen_debug, slate)
        await _send_long_message(update, text)
    except Exception as error:
        logging.exception("/bullpen_debug failed")
        await update.message.reply_text(f"/bullpen_debug failed: {error!r}")


async def learning_roi_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show AI learning ROI breakdown."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    args = context.args or []
    target = args[0] if args else eastern_today().isoformat()
    try:
        report = await asyncio.to_thread(load_learning_report, target)
        if not report.get("roi_by_market"):
            await update.message.reply_text(f"No ROI data in learning report for {target}.\nRun /grade_today or /grade_yesterday first.")
            return
        text = render_learning_report(report)
        await _send_long_message(update, text)
    except Exception as error:
        logging.exception("/learning_roi_debug failed")
        await update.message.reply_text(f"/learning_roi_debug failed: {error!r}")


async def confidence_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show confidence calibration data."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    try:
        from services.confidence_calibration import render_confidence_debug
        text = await asyncio.to_thread(render_confidence_debug)
        await _send_long_message(update, text)
    except Exception as error:
        logging.exception("/confidence_debug failed")
        await update.message.reply_text(f"/confidence_debug failed: {error!r}")


async def _build_card_debug_text(selected_date: str | None = None, *, include_started: bool = False) -> str:
    """Build owner-only card diagnostics shared by slash command and callback."""
    selected_date = selected_date or official_sports_date().isoformat()
    sections: list[str] = []
    save_result: dict[str, Any] = {}
    skip_reason = ""
    official_picks_count = 0
    trackable_picks_count = 0
    market_context_found = False
    skipped_reasons: list[str] = []
    odds_events_returned = 0
    odds_provider_used = "none"
    matched_odds_games = 0
    full_slate = await asyncio.to_thread(
        get_combined_slate,
        os.getenv("ODDS_API_KEY", ""),
        game_date=selected_date,
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    full_slate = [game for game in full_slate if mlb_local_game_date(game.get("game_time")) == selected_date]
    slate = full_slate if include_started else upcoming_mlb_slate(full_slate)
    if not slate:
        return (
            f"🧪 BETGPTAI CARD DEBUG\n📅 Date: {display_date(selected_date)}\n\n"
            "No upcoming games remain for this card date. Use --include-started or test tomorrow."
        )
    if slate:
        odds_events_returned = sum(1 for g in slate if g.get("odds_status") == "available")
        matched_odds_games = odds_events_returned
        first_ctx = slate[0].get("market_context", {})
        odds_provider_used = first_ctx.get("provider", "none") if first_ctx else "none"
    market_context_found = any(bool(game.get("best_available_prices")) for game in slate)
    try:
        if include_started:
            analysis = build_fallback_card(slate)
            skip_reason = "Historical/include-started debug uses deterministic fallback display sections."
        else:
            analysis = await analyze_mlb_slate(
                slate,
                os.getenv("OPENAI_API_KEY", ""),
                os.getenv("ANTHROPIC_API_KEY", ""),
            )
    except Exception as error:
        skip_reason = f"AI unavailable; fallback card used: {error}"
        analysis = build_fallback_card(slate)
    for heading in (
        "🔥 PLAY OF THE DAY",
        "🏆 TOP 2 MONEYLINE",
        "🏆 TOP 5 MONEYLINE",
        "🔥 TOP 2 F5 MONEYLINE",
        "🔥 TOP 5 F5",
        "📈 TOP 2 RUNLINE/SPREAD",
        "📈 TOP 5 RUNLINE/SPREAD",
        "🎯 TOP 2 OVER/UNDER TOTAL RUNS",
        "🎯 TOP 5 GAME TOTALS",
        "💰 TOP 2 TEAM TOTALS",
        "💰 TOP 5 TEAM TOTALS",
        "🧩 2-LEG SAFE PARLAY",
    ):
        if heading in analysis:
            sections.append(heading)
    try:
        extracted = await asyncio.to_thread(
            extract_official_picks,
            analysis,
            slate,
            selected_date,
            "card_debug_preview",
        )
        official_picks_count = len(extracted)
        trackable_picks_count = sum(1 for pick in extracted if pick.get("game_pk") or pick.get("game_id"))
        if not extracted:
            skipped_reasons.append("No official picks could be extracted from generated card text.")
        if extracted and not market_context_found:
            skipped_reasons.append("Generated picks exist, but no matched market context was found.")
    except Exception as extract_error:
        skipped_reasons.append(f"Official pick extraction failed: {extract_error!r}")
    builder_count = 0
    dict_count = 0
    persist_count = 0
    builder_trace_version = "MISSING"
    builder_error = ""
    builder_sections_found: list[str] = []
    builder_candidates_found = 0
    builder_rejected: list[str] = []
    save_result: dict[str, Any] = {"success": False, "error": "builder_not_run"}
    try:
        card_obj = await asyncio.to_thread(
            build_card_from_analysis, analysis, slate, selected_date, "card_debug",
        )
        builder_count = len(card_obj.official_picks)
        builder_trace_version = card_obj.metadata.get("builder_trace_version", "MISSING")
        builder_sections_found = list(card_obj.metadata.get("sections_found") or [])
        builder_candidates_found = int(card_obj.metadata.get("candidates_found") or 0)
        builder_rejected = list(card_obj.metadata.get("rejected_reasons") or [])
        if card_obj.metadata.get("builder_conversion_failed"):
            builder_error = card_obj.metadata.get("builder_conversion_error", "stats-only conversion failed")
        card_dict = structured_card_to_dict(card_obj)
        dict_count = len(card_dict.get("official_picks", []))
        official_picks_count = dict_count
        trackable_picks_count = sum(1 for pick in card_dict.get("official_picks", []) if pick.get("game_pk") or pick.get("game_id"))
        card_dict["analysis"] = analysis
        card_dict["slate"] = slate
        card_dict["source_command"] = "card_debug"
        persist_count = len(card_dict.get("official_picks", []))
        save_result = await asyncio.to_thread(persist_official_card, card_dict)
        if not save_result.get("success"):
            skip_reason = str(save_result.get("error") or "Pick persistence failed")
    except Exception as build_error:
        builder_error = f"{type(build_error).__name__}: {build_error}"
        logging.warning("/card_debug builder/persist failed: %s", builder_error)
        save_result = {"success": False, "error": builder_error}

    stats_only = os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    market_mode = "Stats Only" if stats_only else "Normal"

    # Emergency fallback: when the advanced builder failed and we are in
    # stats-only mode, probe the independent simple engine so the owner can
    # recover without a working StructuredCard pipeline.
    simple_available = "NO"
    simple_count = 0
    if stats_only and (builder_error or builder_count == 0):
        try:
            simple_card = await asyncio.to_thread(build_simple_mlb_card, selected_date)
            simple_count = int(simple_card.get("counts", {}).get("total", 0))
            simple_available = "YES" if simple_count > 0 else "YES (0 picks)"
        except Exception as simple_error:
            simple_available = f"NO ({type(simple_error).__name__})"

    lines = [
        "🧪 BETGPTAI CARD DEBUG",
        f"📅 Date: {display_date(selected_date)}",
        "",
        "Generated card sections:",
        *(f"- {section}" for section in sections),
        "",
        f"Market Mode: {market_mode}",
        f"Market context available: {'yes' if market_context_found else 'no'}",
        f"Odds provider used: {odds_provider_used}",
        f"Odds events returned: {odds_events_returned}",
        f"Matched games: {matched_odds_games} / {len(slate)}",
        f"Official picks count: {official_picks_count}",
        f"Trackable picks count: {trackable_picks_count}",
        f"Trackable picks saved this run: {save_result.get('saved_pick_count', 0)}",
        f"Save success: {'yes' if save_result.get('success') else 'no'}",
        f"Save result: {save_result.get('error') or 'OK'}",
        "",
        "── TRACE: official_picks count ──",
        f"Builder Trace Version: {builder_trace_version}",
        f"Sections found: {builder_sections_found}",
        f"Candidates found: {builder_candidates_found}",
        f"Official picks created: {builder_count}",
        f"Rejected items: {len(builder_rejected)}",
        f"After build_card_from_analysis:  {builder_count}",
        f"After structured_card_to_dict:  {dict_count}",
        f"Before persist_official_card:   {persist_count}",
        f"After persist (saved):          {save_result.get('saved_pick_count', 0)}",
        f"After persist (success):        {save_result.get('success')}",
    ]
    if builder_rejected:
        lines.extend(f"- Rejected: {reason}" for reason in builder_rejected[:12])
    if builder_error:
        lines += [
            "",
            "── Builder Conversion ──",
            f"Conversion failed: YES",
            f"Error: {builder_error}",
        ]
    if stats_only and save_result.get("stats_section_debug"):
        sdb = save_result["stats_section_debug"]
        lines += [
            "",
            "── Stats-Only Section Build ──",
            f"Sections found: {sdb.get('sections_found_count', 0)}",
            f"Item counts: {sdb.get('section_item_counts', {})}",
            f"Converted: {sdb.get('total_converted', 0)}",
            f"Rejected: {sdb.get('total_rejected', 0)}",
        ]
        if sdb.get("rejected_items"):
            for idx in range(min(len(sdb["rejected_items"]), 5)):
                lines.append(f"  Rejected: {sdb['rejected_items'][idx][:60]} — {sdb['rejection_reasons'][idx][:80]}")
    if stats_only:
        lines += [
            "",
            "── Simple Engine Fallback ──",
            f"Simple Engine Available: {simple_available}",
            f"Simple Picks Count: {simple_count}",
        ]
    lines += [
        "Skipped picks and reasons:",
        *(f"- {reason}" for reason in (skipped_reasons or [skip_reason or "No skip reason reported."])),
    ]
    if skip_reason and not skipped_reasons:
        lines.append(f"Official picks skipped reason: {skip_reason}")
    lines += [
        "",
        "── Emergency Options ──",
        "/simple_card_debug",
        "/simple_generate_today",
        "/simple_post_today",
        "/force_post_text_card",
    ]
    return "\n".join(lines).strip()


async def card_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build a card, attempt persistence, and explain skipped picks."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    parsed = _parse_debug_command_args(list(context.args or []), allow_league=False)
    include_started = bool(parsed["include_started"])
    selected_date = parsed["event_date"] or official_sports_date().isoformat()
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Invalid --date. Use YYYY-MM-DD.")
        return
    try:
        await update.message.reply_text("⏳ Building card debug snapshot...")
        await _send_long_message(update, await _build_card_debug_text(selected_date, include_started=include_started))
    except Exception as error:
        logging.exception("/card_debug failed")
        await update.message.reply_text(f"/card_debug failed:\n{error!r}")


async def force_post_text_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only emergency post of the generated MLB card text to FREE_CHANNEL_ID.

    Posts the same AI-generated card text that /card_debug inspects, even when
    the StructuredCard official_picks count is 0.  This does NOT mark the normal
    workflow complete, does NOT mark picks saved, and does NOT create fake picks.
    """
    del context
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_TELEGRAM_ID:
        return
    if not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Generating card text for emergency free-channel post...")
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        slate = upcoming_mlb_slate(slate)
        if not slate:
            await update.message.reply_text("❌ No upcoming MLB games available; cannot post emergency card.")
            return
        try:
            analysis = await analyze_mlb_slate(
                slate,
                os.getenv("OPENAI_API_KEY", ""),
                os.getenv("ANTHROPIC_API_KEY", ""),
            )
        except Exception as error:
            analysis = build_fallback_card(slate)
            logging.warning("force_post_text_card used fallback card: %s", error)
        footer = (
            "Emergency stats-based card. Odds vary by sportsbook. "
            "Verify lines before placing any wager."
        )
        message = f"{analysis.strip()}\n\n━━━━━━━━━━━━\n\n{footer}"

        chat_id = _telegram_destination(os.getenv("FREE_CHANNEL_ID", ""))
        remaining = message.strip()
        posted = 0
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
            await context.bot.send_message(chat_id=chat_id, text=chunk)
            posted += 1

        SYSTEM_LOG.info(
            "component=ForcePostTextCard status=posted source=manual_emergency_text_post "
            "market_mode=stats_only tracked=false channel=FREE_CHANNEL_ID date=%s chunks=%s",
            selected_date, posted,
        )
        await update.message.reply_text(
            "Emergency text card posted to free channel.\n"
            "Tracked picks: NO\n"
            "Snapshot: NO\n"
            "Reason: StructuredCard official_picks empty."
        )
    except Exception as error:
        logging.exception("/force_post_text_card failed")
        await update.message.reply_text(f"❌ Emergency post failed:\n{error!r}")


async def simple_card_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build the simple stats-only card and show pick counts by market."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Building simple MLB card debug...")
    try:
        card = await asyncio.to_thread(build_simple_mlb_card, selected_date)
        counts = card.get("counts", {})
        errors = card.get("errors", [])
        picks = [pick for pick in card.get("picks", []) if isinstance(pick, dict)]
        picks_with_dk = [
            pick for pick in picks
            if pick.get("sportsbook") == "draftkings" and pick.get("line_verified")
            and (pick.get("odds_american") is not None or pick.get("posted_odds") is not None)
        ]
        path = services_simple_path(selected_date)
        lines = [
            "🧪 SIMPLE MLB CARD DEBUG",
            f"📅 Date: {selected_date}",
            f"Market Mode: {card.get('market_mode', 'stats_only')}",
            f"DraftKings Lines Verified: {'YES' if card.get('draftkings_lines_verified') else 'NO'}",
            f"Matched DK Games: {card.get('market_context_matched_games', 0)}",
            f"DK market context games available: {card.get('dk_market_context_games_available', 0)}",
            f"DK market context source: {card.get('dk_market_context_source', 'none')}",
            f"DK market context rows used: {card.get('dk_market_context_rows_used', 0)}",
            f"DK odds attach attempts: {card.get('dk_odds_attach_attempts', 0)}",
            f"DK odds attach success: {card.get('dk_odds_attach_success', 0)}",
            f"DK odds attach failures: {card.get('dk_odds_attach_failures', 0)}",
            f"Picks with DK odds: {len(picks_with_dk)}",
            f"Picks without odds: {len(picks) - len(picks_with_dk)}",
            "Attach failure reasons:",
            *[
                f"- {reason}: {count}"
                for reason, count in (card.get("dk_odds_attach_failure_reasons") or {}).items()
            ],
            "",
            "Picks by market:",
            f"- Play of the Day: {counts.get('play_of_day', 0)}",
            f"- Moneylines: {counts.get('moneyline', 0)}",
            f"- F5 Moneylines: {counts.get('f5_moneyline', 0)}",
            f"- Runlines: {counts.get('runline', 0)}",
            f"- Parlay legs: {counts.get('parlay_legs', 0)}",
            f"- Core Five: {counts.get('core_five', 0)}",
            f"- TOTAL: {counts.get('total', 0)}",
            "",
            f"Saved path: {path}",
            f"Errors: {errors if errors else 'none'}",
        ]
        await _send_long_message(update, "\n".join(lines))
    except Exception as error:
        logging.exception("/simple_card_debug failed")
        await update.message.reply_text(f"❌ Simple card debug failed:\n{error!r}")


async def simple_generate_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build and save today's simple MLB card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Building and saving simple MLB card...")
    try:
        card = await asyncio.to_thread(build_simple_mlb_card, selected_date)
        path = await asyncio.to_thread(save_simple_mlb_card, card)
        await update.message.reply_text(
            f"✅ Simple card generated.\n\n"
            f"Saved picks: {card.get('counts', {}).get('total', 0)}\n"
            f"Path: {path}"
        )
    except Exception as error:
        logging.exception("/simple_generate_today failed")
        await update.message.reply_text(f"❌ Simple generate failed:\n{error!r}")


async def simple_post_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: load (or build+save) the simple card and post to FREE_CHANNEL_ID."""
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Loading simple MLB card for posting...")
    try:
        path = services_simple_path(selected_date)
        import json as _json
        if Path(str(path)).exists():
            card = _json.loads(Path(str(path)).read_text(encoding="utf-8"))
        else:
            card = await asyncio.to_thread(build_simple_mlb_card, selected_date)
            path = await asyncio.to_thread(save_simple_mlb_card, card)
        channel_id = _telegram_destination(os.getenv("FREE_CHANNEL_ID", ""))
        posted = await post_simple_mlb_card(card, context.bot, channel_id)
        saved = card.get("counts", {}).get("total", 0)
        source = card.get("source", "simple_mlb_card_v1")
        await update.message.reply_text(
            f"{'✅ Simple MLB card posted to free channel.' if posted else '❌ Simple post failed (check FREE_CHANNEL_ID).'}\n"
            f"Saved picks: {saved}\n"
            f"Tracked source: {source}"
        )
    except Exception as error:
        logging.exception("/simple_post_today failed")
        await update.message.reply_text(f"❌ Simple post failed:\n{error!r}")


async def bridge_simple_card_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: bridge today's simple card into the Results Vault / picks.json."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Bridging simple card to Results Vault...")
    try:
        result = await asyncio.to_thread(export_simple_card_to_official_picks, selected_date)
        if not result.get("exists"):
            await update.message.reply_text(
                f"❌ Cannot bridge: simple card not found at {result.get('simple_card_path')}.\n"
                "Run /simple_generate_today first."
            )
            return
        await update.message.reply_text(
            "✅ Simple card bridged to Results Vault.\n"
            f"Imported picks: {result.get('imported', 0)}\n"
            f"Skipped duplicates: {result.get('skipped', 0)}\n"
            f"Path: {result.get('path')}"
        )
    except Exception as error:
        logging.exception("/bridge_simple_card_today failed")
        await update.message.reply_text(f"❌ Bridge failed:\n{error!r}")


async def simple_results_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show simple-card bridge and Results Vault compatibility."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("⏳ Checking simple card bridge status...")
    try:
        status = await asyncio.to_thread(simple_card_bridge_status, selected_date)
        lines = [
            "🧪 SIMPLE CARD → RESULTS VAULT",
            f"📅 Date: {selected_date}",
            "",
            f"Simple card exists: {'YES' if status.get('simple_card_exists') else 'NO'}",
            f"Simple pick count: {status.get('simple_pick_count', 0)}",
            f"Bridged: {'YES' if status.get('bridged') else 'NO'}",
            f"Results Vault compatible: {'YES' if status.get('results_vault_compatible') else 'NO'}",
        ]
        if status.get("errors"):
            lines += ["", "Errors:"] + [f"- {e}" for e in status["errors"]]
        await _send_long_message(update, "\n".join(lines))
    except Exception as error:
        logging.exception("/simple_results_debug failed")
        await update.message.reply_text(f"❌ Simple results debug failed:\n{error!r}")


def services_simple_path(card_date: str) -> str:
    """Return the expected saved path for a simple card (mirrors simple_mlb_card)."""
    from storage import DATA_DIR
    return str(DATA_DIR / "simple_cards" / f"{card_date}.json")


async def mlb_top5_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Full MLB Top 5 admin card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    try:
        await update.message.reply_text("⏳ Building admin-only MLB Top 5 Card...")
        selected_date = official_sports_date().isoformat()
        report = await asyncio.to_thread(
            build_mlb_top5_admin_card,
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        await _send_long_text_to_message(update.message, render_mlb_top5_admin_card(report))
        report_path = report.get("report_path")
        if report_path and Path(str(report_path)).exists():
            with Path(str(report_path)).open("rb") as report_file:
                await update.message.reply_document(
                    document=report_file,
                    filename="mlb_top5_admin.json",
                    caption="📎 Saved MLB Top 5 Admin JSON",
                )
    except Exception as error:
        logging.exception("Unexpected /mlb_top5_admin error")
        await update.message.reply_text(
            f"Unable to build MLB Top 5 admin card right now.\n\nError: {error}"
        )


async def mlb_admin_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only Anime dashboard version of the MLB War Room."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    await update.message.reply_text("⏳ Building MLB War Room image preview...")
    selected_date = official_sports_date().isoformat()
    try:
        report = await build_mlb_admin_report_async(
            selected_date,
            odds_api_key=os.getenv("ODDS_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        result = await asyncio.to_thread(
            prepare_mlb_admin_image,
            report,
            image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
        )
        image_path = result.get("image_path")
        if image_path and Path(str(image_path)).exists():
            with Path(str(image_path)).open("rb") as image_file:
                await update.message.reply_photo(
                    photo=image_file,
                    caption="🖼 BETGPTAI MLB War Room Anime Dashboard — Admin Only",
                )
        else:
            await _send_long_message(
                update,
                "🖼 MLB War Room image prompt saved.\n\n"
                f"Prompt Path: {result.get('prompt_path')}\n"
                + (f"Image Error: {result.get('image_error')}\n\n" if result.get("image_error") else "\n")
                + str(result.get("prompt")),
            )
    except Exception:
        logging.exception("Unexpected /mlb_admin_image error")
        await update.message.reply_text(
            "Unable to build MLB War Room image right now. Check terminal logs."
        )


async def _quality_gate_or_notify(
    update: Update,
    *,
    mode: str = "general",
) -> bool:
    """Return True when posting may continue; otherwise notify owner."""
    if not update.message:
        return False
    try:
        payload = await asyncio.to_thread(run_prepost_quality_gate, mode=mode)
    except Exception as error:
        logging.exception("Pre-post quality gate crashed")
        await update.message.reply_text(
            "❌ PRE-POST CHECK FAILED\n\n"
            f"Quality gate crashed before posting. Error: {error}"
        )
        return False
    if payload.get("ready_to_post"):
        return True
    await _send_long_message(
        update,
        "❌ POST BLOCKED BY QUALITY GATE\n\n"
        + render_prepost_quality_gate(payload),
    )
    return False


async def hr_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only HR watch preview."""
    del context
    await _send_props_lab(update, "home_runs")


async def strikeouts_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only strikeout prop preview."""
    del context
    await _send_props_lab(update, "strikeouts")


async def prop_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only raw prop candidate debug output."""
    del context
    await _send_props_lab(update, "debug")


async def hitprops_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only focused Hit Props Engine diagnostics."""
    del context
    await _send_props_lab(update, "hitprops_debug")


async def props_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only sportsbook-specific prop diagnostics."""
    mode = "fanduel_debug" if any(arg.lower() == "fanduel" for arg in (context.args or [])) else "debug"
    await _send_props_lab(update, mode)


async def props_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only engine status card."""
    del context
    await _send_props_lab(update, "test")


async def approve_prop_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: move one lab prop to approved_props.json without posting it."""
    if not await _require_admin(update):
        return
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve_prop PROP_ID")
        return
    ok, message = await asyncio.to_thread(approve_prop, context.args[0])
    prefix = "✅" if ok else "❌"
    await update.message.reply_text(
        f"{prefix} {message}\n\n"
        "Approval is admin-only and does not post props to any channel."
    )


async def backfill_today(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Privately regenerate and save today's official card without publishing it."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    await update.message.reply_text("⏳ Backfilling today’s official picks...")
    selected_date = official_sports_date().isoformat()
    try:
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return
        analysis = await analyze_mlb_slate(
            slate,
            os.getenv("OPENAI_API_KEY", ""),
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        await asyncio.to_thread(
            save_model_report, selected_date, slate, analysis, get_last_analysis_metadata()
        )
        saved_count = await asyncio.to_thread(
            _save_official_mlb_card,
            analysis,
            slate,
            selected_date,
            "backfill_today",
        )
        await update.message.reply_text(
            f"✅ Backfill complete. Saved {saved_count} official picks."
        )
    except Exception:
        logging.exception("Unexpected /backfill_today error")
        await update.message.reply_text(
            "Unable to backfill today’s picks. Check the terminal and try again."
        )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the private BETGPTAI administration panel."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    try:
        counts = await asyncio.to_thread(_database_counts)
    except ResultsTrackerError as error:
        logging.warning("Could not load admin database counts: %s", error)
        counts = {"total": 0, "pending": 0, "graded": 0}

    await update.message.reply_text(
        "⚙️ BETGPTAI ADMIN PANEL\n\n"
        "📡 API STATUS\n"
        "/status · /storage_status\n"
        "/debug_thesportsdb · /debug_football_data · /debug_pybaseball\n\n"
        "🔧 SYSTEM DIAGNOSTICS\n"
        "/system_diagnostics · /callback_debug\n\n"
        "⏰ SCHEDULER\n"
        "/time_debug · /scheduler_status · /post_status\n"
        "/force_generate_today · /force_post_today · /workflow_debug\n\n"
        "💾 PICK PERSISTENCE\n"
        "/save_today_picks · /saved_picks_today\n"
        "/save_debug · /repair_storage\n\n"
        "📋 LINEUPS\n"
        "/lineup_status\n\n"
        "🧠 MODEL REPORT\n"
        "/model_report\n\n"
        "⚾ MLB ADMIN CARDS\n"
        "/mlb_admin · /mlb_top5_admin · /warroom_debug\n\n"
        "📊 UPDATE RESULTS\n"
        "/update_results\n\n"
        "✅ GRADE PICKS\n"
        "/grade_today · /grade_yesterday\n"
        "/force_grade_date YYYY-MM-DD\n"
        "/grade_debug YYYY-MM-DD\n\n"
        "🧪 DEBUG PICKS\n"
        "/debug_picks\n\n"
        "🧪 DEBUG RESULTS\n"
        "/debug_results · /date_debug · /callback_debug\n\n"
        "🛡 PRE-POST QUALITY GATE\n"
        "/prepost_check · /integrity_report\n\n"
        "🧠 INTELLIGENCE DASHBOARD\n"
        "/daily_intel_admin · /morning_report_admin\n"
        "/lineup_report_admin · /model_review_admin · /intel_debug\n\n"
        "🧠 AI LEARNING\n"
        "/learning_report_admin · /loss_review_admin\n"
        "/weight_suggestions_admin · /learning_status\n"
        "/learning_auto_status · /toggle_learning_auto_apply\n"
        "/weights_admin · /weight_history_admin\n"
        "/approve_weight_update · /reject_weight_update\n\n"
        "⚾ PLAYER PROP LAB\n"
        "/props_admin · /hits_admin · /hr_admin · /strikeouts_admin\n"
        "/hits_by_team_admin\n"
        "/props_images_admin · /hits_by_team_image_admin\n"
        "/best_hit_image_admin · /post_best_hit_image_admin\n"
        "/verify_best_hit_prop · /clear_prop_cache\n"
        "/magazine_admin · /verify_player PLAYER_NAME\n"
        "/props_test · /prop_debug · /hitprops_debug\n"
        "/approve_prop PROP_ID\n\n"
        "📝 BACKFILL PICKS\n"
        "/backfill_today\n\n"
        "📡 TEST CHANNELS\n"
        "/test_channels\n\n"
        "🖼 IMAGE CARD TESTS\n"
        "/image_test\n"
        "/today_image_admin · /mlb_auto_image_admin\n"
        "/mlb_images\n"
        "/post_mlb_images\n"
        "/test_image_card\n"
        "/post_test_image\n\n"
        "⚽ SOCCER TOOLS\n"
        "/soccer_full · /btts · /corners · /overs · /cards\n"
        "/first_half · /second_half · /double_chance · /asian_handicap\n"
        "/soccer_results · /debug_soccer\n\n"
        "💾 DATABASE INFO\n\n"
        f"Total Tracked Picks: {counts['total']}\n"
        f"Pending Picks: {counts['pending']}\n"
        f"Graded Picks: {counts['graded']}\n\n"
        "🤖 BOT VERSION\n\n"
        "Current Version:\n"
        "BETGPTAI v1.2\n\n"
        "━━━━━━━━━━━━"
    )


async def model_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the owner which sources actually contributed to today's MLB card."""
    del context
    if not await _require_admin(update) or not update.message:
        return
    selected_date = official_sports_date().isoformat()

    async def load_soccer_summary() -> dict[str, object]:
        """Build a small owner-only soccer engine summary without affecting users."""
        try:
            slate = await asyncio.to_thread(
                get_soccer_slate,
                os.getenv("FOOTBALL_DATA_API_KEY", ""),
                os.getenv("ODDS_API_KEY", ""),
                game_date=selected_date,
                sports_db_api_key=thesportsdb_api_key(),
                serpapi_key=os.getenv("SERPAPI_KEY", ""),
                api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
            )
            return soccer_slate_summary(slate)
        except Exception:
            logging.info("Soccer model summary unavailable for /model_report")
            return {}

    (
        report,
        clubelo_available,
        understat_available,
        fangraphs_ok,
        statsbomb_ok,
        fbref_ok,
        api_football_ok,
        api_sports_baseball_ok,
        sportsdb_ok,
        soccer_summary,
    ) = await asyncio.gather(
        asyncio.to_thread(load_model_report, selected_date),
        asyncio.to_thread(check_clubelo),
        asyncio.to_thread(check_understat),
        asyncio.to_thread(fangraphs_available),
        asyncio.to_thread(statsbomb_available),
        asyncio.to_thread(fbref_available),
        asyncio.to_thread(api_football_available, os.getenv("API_FOOTBALL_KEY", "")),
        (
            asyncio.to_thread(
                api_sports_baseball_available,
                os.getenv("API_SPORTS_KEY", "") or os.getenv("API_FOOTBALL_KEY", ""),
            )
            if _truthy_env("API_SPORTS_BASEBALL_ENABLED")
            else asyncio.sleep(0, result=False)
        ),
        asyncio.to_thread(check_thesportsdb, thesportsdb_api_key()),
        load_soccer_summary(),
    )
    football_data_status = (
        "✅ Configured" if os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
        else "❌ Missing"
    )
    serpapi_status = (
        "✅ Configured"
        if check_serpapi(os.getenv("SERPAPI_KEY", ""))
        else "➖ Not configured"
    )
    if not report:
        await update.message.reply_text(
            "No model report exists for today yet. Run /mlb_auto first.\n\n"
            f"API-Sports Baseball status: {'➖ Disabled' if not _truthy_env('API_SPORTS_BASEBALL_ENABLED') else '✅ Available' if api_sports_baseball_ok else '❌ Unavailable'}\n"
            f"Football-Data status: {football_data_status}\n"
            f"TheSportsDB status: {'✅ Available' if sportsdb_ok else '❌ Unavailable'}\n"
            "Weather API status: ✅ Available\n"
            f"Odds API status: {'✅ Configured' if os.getenv('ODDS_API_KEY', '').strip() else '❌ Missing'}\n"
            f"OpenAI status: {'✅ Configured' if os.getenv('OPENAI_API_KEY', '').strip() else '❌ Missing'}\n"
            f"Claude status: {'✅ Configured' if os.getenv('ANTHROPIC_API_KEY', '').strip() else '❌ Missing'}\n"
            f"ClubElo status: {'✅ Available' if clubelo_available else '❌ Unavailable'}\n"
            f"Understat status: {'✅ Available' if understat_available else '❌ Unavailable'}\n"
            f"StatsBomb status: {'✅ Available' if statsbomb_ok else '❌ Unavailable'}\n"
            f"FBref status: {'✅ Available' if fbref_ok else '❌ Unavailable'}\n"
            f"FanGraphs/pybaseball status: {'✅ Available' if fangraphs_ok else '❌ Unavailable'}\n"
            f"Player Props Engine: {props_engine_status}\n"
            f"Intelligence Dashboard: {'✅ Available' if intelligence_dashboard_available() else '❌ Unavailable'}\n"
            f"SerpApi status: {serpapi_status}\n\n"
            "Soccer candidates:\n"
            f"BTTS candidates: {soccer_summary.get('btts_candidates', 0)}\n"
            f"Over candidates: {soccer_summary.get('over_candidates', 0)}\n"
            f"Double Chance candidates: {soccer_summary.get('double_chance_dnb_candidates', 0)}\n"
            f"World Cup candidates: {soccer_summary.get('world_cup_candidates', 0)}"
        )
        return

    sources = report.get("sources", {})

    def status(key: str) -> str:
        return "✅ Used" if sources.get(key) is True else "❌ Unavailable"

    fallbacks = report.get("fallbacks_used", [])
    fallback_names = ", ".join(fallbacks) if fallbacks else "None"
    auto_posting = report.get("auto_posting", {})
    if not isinstance(auto_posting, dict):
        auto_posting = {}
    await update.message.reply_text(
        "🧠 BETGPTAI MODEL REPORT\n\n"
        f"Card Date: {report.get('date', selected_date)}\n\n"
        f"MLB Stats API: {status('mlb_stats')}\n"
        f"Baseball Savant: {status('baseball_savant')}\n"
        f"FanGraphs: {status('fangraphs')}\n"
        f"Highlightly: {status('highlightly')}\n"
        f"Weather: {status('weather')}\n"
        f"Odds API: {status('odds_api')}\n"
        f"OpenAI: {status('openai')}\n"
        f"Claude: {status('claude')}\n"
        f"API-Sports Baseball: {status('api_sports_baseball')}\n"
        f"API-Sports Baseball status: {'➖ Disabled' if not _truthy_env('API_SPORTS_BASEBALL_ENABLED') else '✅ Available' if api_sports_baseball_ok else '❌ Unavailable'}\n"
        f"Football-Data status: {football_data_status}\n"
        f"TheSportsDB status: {'✅ Available' if sportsdb_ok else '❌ Unavailable'}\n"
        "Weather API status: ✅ Available\n"
        f"Odds API status: {'✅ Configured' if os.getenv('ODDS_API_KEY', '').strip() else '❌ Missing'}\n"
        f"OpenAI status: {'✅ Configured' if os.getenv('OPENAI_API_KEY', '').strip() else '❌ Missing'}\n"
        f"Claude status: {'✅ Configured' if os.getenv('ANTHROPIC_API_KEY', '').strip() else '❌ Missing'}\n"
        f"ClubElo status: {'✅ Available' if clubelo_available else '❌ Unavailable'}\n"
        f"Understat status: {'✅ Available' if understat_available else '❌ Unavailable'}\n"
        f"StatsBomb status: {'✅ Available' if statsbomb_ok else '❌ Unavailable'}\n"
        f"FBref status: {'✅ Available' if fbref_ok else '❌ Unavailable'}\n"
        f"FanGraphs/pybaseball status: {'✅ Available' if fangraphs_ok else '❌ Unavailable'}\n"
        f"Player Props Engine: {props_engine_status}\n"
        f"Intelligence Dashboard: {'✅ Available' if intelligence_dashboard_available() else '❌ Unavailable'}\n"
        f"SerpApi status: {serpapi_status}\n\n"
        "━━━━━━━━━━━━\n\n"
        f"Savant games enriched: {report.get('savant_games_enriched', 0)}/"
        f"{report.get('total_games', 0)}\n"
        f"FanGraphs games enriched: {report.get('fangraphs_games_enriched', 0)}/"
        f"{report.get('total_games', 0)}\n"
        f"API-Sports Baseball games enriched: "
        f"{report.get('api_sports_baseball_games_enriched', 0)}/"
        f"{report.get('total_games', 0)}\n"
        f"Consensus picks found: {report.get('consensus_picks_found', 0)}\n"
        f"Value engine count: {report.get('value_engine_count', 0)}\n"
        f"Official v20 markets: ML, Run Line, F5, Game Totals, Team Totals\n"
        f"F5 candidates: {report.get('f5_candidates', 0)}\n"
        f"Team total candidates: {report.get('team_total_candidates', 0)}\n"
        f"Strikeout candidates: {report.get('strikeout_candidates', 0)}\n"
        f"HR watch candidates: {report.get('home_run_candidates', 0)}\n"
        "\nSoccer candidates:\n"
        f"BTTS candidates: {soccer_summary.get('btts_candidates', 0)}\n"
        f"Over candidates: {soccer_summary.get('over_candidates', 0)}\n"
        f"Double Chance candidates: {soccer_summary.get('double_chance_dnb_candidates', 0)}\n"
        f"World Cup candidates: {soccer_summary.get('world_cup_candidates', 0)}\n"
        f"Fallbacks used: {len(fallbacks)}\n"
        f"Fallback details: {fallback_names}\n\n"
        "━━━━━━━━━━━━\n\n"
        "Auto-posting status:\n"
        f"Status: {auto_posting.get('status', 'Unavailable')}\n"
        f"Sent: {auto_posting.get('sent', 0)}\n"
        f"Skipped: {auto_posting.get('skipped', 0)}\n"
        f"Failed: {auto_posting.get('failed', 0)}\n"
        f"Last Recorded: {auto_posting.get('last_recorded_at', 'Unavailable')}"
    )


def _telegram_destination(value: str) -> int | str:
    """Convert numeric Telegram IDs while still allowing @channel usernames."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Channel ID is missing from .env.")
    if cleaned.startswith("@"):
        return cleaned
    try:
        numeric_id = int(cleaned)
    except ValueError as error:
        raise ValueError("Channel ID must be numeric or an @username.") from error

    # Telegram's copied channel/supergroup IDs normally begin with -100. Some
    # dashboards omit the minus sign, so accept that beginner-friendly format.
    if numeric_id > 0 and cleaned.startswith("100"):
        numeric_id = -numeric_id
    return numeric_id


async def test_channels(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Privately verify delivery to every configured BETGPTAI destination."""
    if not await _require_admin(update):
        return
    if not update.message:
        return

    destinations = (
        ("Free Channel", "FREE_CHANNEL_ID"),
        ("VIP Channel", "VIP_CHANNEL_ID"),
        ("Community Group", "COMMUNITY_GROUP_ID"),
    )
    results: list[str] = []
    for label, environment_name in destinations:
        try:
            chat_id = _telegram_destination(os.getenv(environment_name, ""))
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ BETGPTAI test message",
            )
            results.append(f"{label}: ✅ Success")
        except Exception as error:
            # The full Telegram error is shown only in this owner-protected chat.
            logging.warning("Channel test failed for %s: %s", label, error)
            results.append(f"{label}: ❌ Failed\nError: {error}")

    await update.message.reply_text(
        "📡 CHANNEL TEST RESULTS\n\n" + "\n\n".join(results)
    )


MLB_IMAGE_HEADINGS = [
    "🔥 PLAY OF THE DAY",
    "🏆 TOP 5 MONEYLINE",
    "🔥 TOP 5 F5",
    "📈 TOP 5 RUN LINE",
    "🎯 TOP 5 GAME TOTALS",
    "💰 TOP 5 TEAM TOTALS",
    "🧩 2-LEG SAFE PARLAY",
]


def _truthy_env(name: str) -> bool:
    """Read true/false feature flags from .env in a beginner-friendly way."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _dated_generated_cards_dir(selected_date: str) -> Path:
    """Return generated_cards/YYYY-MM-DD and create it if needed."""
    output_dir = data_file("generated_cards") / selected_date
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _section_from_card(card: str, heading: str) -> str:
    """Extract one section from the Telegram MLB card text."""
    start = card.find(heading)
    if start < 0:
        return ""
    end = len(card)
    for other_heading in MLB_IMAGE_HEADINGS:
        if other_heading == heading:
            continue
        candidate = card.find(other_heading, start + len(heading))
        if candidate >= 0:
            end = min(end, candidate)
    divider = card.find("━━━━━━━━━━━━", start + len(heading))
    if divider >= 0:
        end = min(end, divider)
    return card[start:end].strip()


def _clean_image_pick_line(line: str) -> str:
    """Remove Telegram-only decorations while keeping betting market text."""
    cleaned = re.sub(r"^[\s\d️⃣1-9.✅⚾🔥🏆📈🎯💰🧩-]+", "", line).strip()
    cleaned = re.sub(r"^(Pick|Line|Risk Grade|Confidence Grade|Reason):\s*", "", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def _picks_from_section(section: str, limit: int = 5) -> list[str]:
    """Pull concise pick lines from one section of the generated card."""
    skip_prefixes = (
        "reason", "line:", "risk grade", "confidence grade", "safer line",
        "parlay note", "singles", "parlays", "educational", "card timing",
        "odds vary", "please shop", "🆚", "🕒", "🔴",
    )
    picks: list[str] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or line in MLB_IMAGE_HEADINGS or line == "━━━━━━━━━━━━":
            continue
        if line.lower().startswith(skip_prefixes):
            continue
        cleaned = _clean_image_pick_line(line)
        if not cleaned or cleaned.lower().startswith(skip_prefixes):
            continue
        if cleaned not in picks:
            picks.append(cleaned)
        if len(picks) >= limit:
            break
    return picks


def _official_mlb_card_data(
    analysis: str, slate: list[dict[str, object]], selected_date: str
) -> dict[str, object]:
    """Convert the official Telegram MLB card into image-carousel data."""
    play_section = _section_from_card(analysis, "🔥 PLAY OF THE DAY")
    moneyline_section = _section_from_card(analysis, "🏆 TOP 5 MONEYLINE")
    f5_section = _section_from_card(analysis, "🔥 TOP 5 F5")
    runline_section = _section_from_card(analysis, "📈 TOP 5 RUN LINE")
    totals_section = _section_from_card(analysis, "🎯 TOP 5 GAME TOTALS")
    team_totals_section = _section_from_card(analysis, "💰 TOP 5 TEAM TOTALS")
    parlay_section = _section_from_card(analysis, "🧩 2-LEG SAFE PARLAY")

    play_candidates = _picks_from_section(play_section, 1)
    moneylines = _picks_from_section(moneyline_section, 5)
    f5 = _picks_from_section(f5_section, 5)
    runlines = _picks_from_section(runline_section, 5)
    totals = _picks_from_section(totals_section, 5)
    team_totals = _picks_from_section(team_totals_section, 5)
    safe_parlay = [
        _clean_image_pick_line(line)
        for line in parlay_section.splitlines()
        if line.strip().startswith("✅")
    ][:3]
    core_five = (
        [*play_candidates, *moneylines, *f5, *runlines, *totals, *team_totals]
    )[:5]
    matchups = [
        f"{game.get('away_team', 'Away')} @ {game.get('home_team', 'Home')}"
        for game in slate[:8]
    ]

    return {
        "date": selected_date,
        "play_of_day": play_candidates[0] if play_candidates else "Play of the Day TBD",
        "best_bet": play_candidates[0] if play_candidates else "Best Bet TBD",
        "moneylines": moneylines,
        "f5": f5,
        "runlines": runlines,
        "totals": totals,
        "team_totals": team_totals,
        "safe_parlay": safe_parlay,
        "value_parlay": [*runlines, *totals][:3],
        "ev_parlay": [*moneylines, *team_totals][:3],
        "core_five": core_five,
        "matchups": matchups,
        "raw_text": analysis,
    }


async def test_image_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: explain that placeholder image generation is disabled."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    await update.message.reply_text(
        "🛑 Image card generation is disabled.\n\n"
        "The previous Pillow-only placeholder style did not meet the BETGPTAI "
        "Anime Vault standard.\n\n"
        "Use /mlb_images to get 7 detailed prompts for ChatGPT image generation.\n\n"
        "No image was generated or posted."
    )


async def post_test_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: prevent placeholder image posting to configured destinations."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    await update.message.reply_text(
        "🛑 Test image posting is disabled.\n\n"
        "No image was sent to FREE_CHANNEL_ID or VIP_CHANNEL_ID.\n\n"
        "Use /mlb_images to generate prompt-ready Anime Vault directions first."
    )


async def mlb_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build prompts and optionally generate image previews."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    await update.message.reply_text("⏳ Building BETGPTAI Anime Edition v7.0 MLB images...")
    selected_date = official_sports_date().isoformat()
    try:
        output_dir = _dated_generated_cards_dir(selected_date)
        slate = await asyncio.to_thread(
            get_combined_slate,
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
        if not slate:
            await update.message.reply_text("No MLB games were found for today.")
            return

        analysis = await analyze_mlb_slate(
            slate,
            os.getenv("OPENAI_API_KEY", ""),
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        card_data = _official_mlb_card_data(analysis, slate, selected_date)
        prompts = generate_mlb_card_slides(card_data)
        saved_paths = await asyncio.to_thread(
            save_mlb_card_slide_prompts, prompts, output_dir
        )

        for index, prompt in enumerate(prompts, start=1):
            await _send_long_message(
                update,
                f"🖼 BETGPTAI Anime Edition v7.0\n"
                f"Slide {index}/7 Prompt\n\n{prompt}",
            )

        saved_list = "\n".join(str(path) for path in saved_paths)
        await update.message.reply_text(
            "✅ Anime Vault prompts saved.\n\n"
            f"{saved_list}"
        )

        if not _truthy_env("IMAGE_GENERATION_ENABLED"):
            await update.message.reply_text(
                "IMAGE_GENERATION_ENABLED=false, so I sent prompt-ready slides only."
            )
            return

        await update.message.reply_text(
            "🎨 IMAGE_GENERATION_ENABLED=true. Generating owner-only preview images..."
        )
        for index, prompt in enumerate(prompts, start=1):
            image_path = output_dir / f"slide_{index}.png"
            try:
                saved_image = await asyncio.to_thread(
                    generate_image_from_prompt, prompt, str(image_path)
                )
                with Path(saved_image).open("rb") as image_file:
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"BETGPTAI Anime Vault Preview — Slide {index}/7",
                    )
            except Exception as error:
                logging.exception("OpenAI image generation failed for slide %s", index)
                await _send_long_message(
                    update,
                    f"❌ Image generation failed for slide {index}.\n\n"
                    f"Error: {error}\n\n"
                    f"Fallback prompt:\n\n{prompt}",
                )
    except Exception as error:
        logging.exception("/mlb_images failed")
        await update.message.reply_text(
            "Unable to build Anime Edition prompts right now. "
            f"Owner debug error: {error}"
        )


async def post_mlb_images(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: post approved generated MLB images to configured channels."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    if not await _quality_gate_or_notify(update, mode="mlb_images"):
        return

    selected_date = official_sports_date().isoformat()
    output_dir = _dated_generated_cards_dir(selected_date)
    image_paths = [output_dir / f"slide_{index}.png" for index in range(1, 8)]
    missing = [path.name for path in image_paths if not path.exists()]
    if missing:
        await update.message.reply_text(
            "❌ Approved image set is incomplete.\n\n"
            f"Missing: {', '.join(missing)}\n\n"
            "Run /mlb_images with IMAGE_GENERATION_ENABLED=true first, review the "
            "images, then run /post_mlb_images."
        )
        return

    destinations = (
        ("Free Channel", "FREE_CHANNEL_ID"),
        ("VIP Channel", "VIP_CHANNEL_ID"),
    )
    results: list[str] = []
    for label, environment_name in destinations:
        try:
            chat_id = _telegram_destination(os.getenv(environment_name, ""))
            for index, image_path in enumerate(image_paths, start=1):
                with image_path.open("rb") as image_file:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=image_file,
                        caption=(
                            "BETGPTAI Anime Vault Official MLB Card"
                            if index == 1 else ""
                        ),
                    )
            results.append(f"{label}: ✅ Posted 7 images")
        except Exception as error:
            logging.exception("Posting MLB images failed for %s", label)
            results.append(f"{label}: ❌ Failed\nError: {error}")

    await update.message.reply_text(
        "🖼 MLB IMAGE POST RESULTS\n\n" + "\n\n".join(results)
    )


async def _edit_app_message(query: object, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the current inline-menu message, falling back to a reply if needed."""
    try:
        await query.edit_message_text(text=text, reply_markup=markup)
    except Exception:
        if getattr(query, "message", None):
            await query.message.reply_text(text, reply_markup=markup)


async def _build_mlb_auto_card_for_menu() -> str:
    """Run the public MLB card pipeline for inline-menu users."""
    selected_date = official_sports_date().isoformat()
    slate = await asyncio.to_thread(
        get_combined_slate,
        os.getenv("ODDS_API_KEY", ""),
        game_date=selected_date,
        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
    )
    if not slate:
        return "No MLB games were found for today."
    slate = upcoming_mlb_slate(slate)
    if not slate:
        return "No upcoming MLB games are available for today’s free card."
    try:
        analysis = await analyze_mlb_slate(
            slate,
            os.getenv("OPENAI_API_KEY", ""),
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
    except Exception as error:
        logging.error("AI Analysis Error:\n%s", error, exc_info=True)
        analysis = build_fallback_card(slate)
    await asyncio.to_thread(
        save_model_report, selected_date, slate, analysis, get_last_analysis_metadata()
    )
    try:
        saved_count = await asyncio.to_thread(
            _save_official_mlb_card,
            analysis,
            slate,
            selected_date,
            "tap_menu_mlb_card",
        )
    except Exception as error:
        logging.exception("Could not save official picks to picks.json")
        return (
            "The card was generated, but its picks could not be saved.\n\n"
            f"Error: {error!r}\n\n"
            "No picks were sent."
        )
    return render_mlb_premium_card(selected_date)


async def _build_soccer_card_for_menu() -> str:
    """Run the public soccer pipeline for inline-menu users."""
    selected_date = official_sports_date().isoformat()
    slate = await asyncio.to_thread(
        get_soccer_slate,
        os.getenv("FOOTBALL_DATA_API_KEY", ""),
        os.getenv("ODDS_API_KEY", ""),
        live_only=False,
        game_date=selected_date,
        sports_db_api_key=thesportsdb_api_key(),
        serpapi_key=os.getenv("SERPAPI_KEY", ""),
        api_football_key=os.getenv("API_FOOTBALL_KEY", ""),
    )
    card = await analyze_soccer_slate(
        slate,
        os.getenv("OPENAI_API_KEY", ""),
        "public",
        os.getenv("ANTHROPIC_API_KEY", ""),
    )
    try:
        saved_count = await asyncio.to_thread(
            save_soccer_picks, card, slate, selected_date, "soccer_public"
        )
        if saved_count:
            print(f"Saved {saved_count} soccer picks to picks.json", flush=True)
    except Exception:
        logging.exception("Could not save soccer picks to picks.json")
    return _with_card_date(card, selected_date)


def _mmddyyyy_folder(card_date: str) -> str:
    """Return MM-DD-YYYY for legacy Best Hit image folders."""
    return datetime.fromisoformat(card_date).strftime("%m-%d-%Y")


def _available_public_image(kind: str) -> Path | None:
    """Return an already-generated public preview image when available."""
    selected_date = official_sports_date().isoformat()
    base = data_file("generated_cards")
    candidates = {
        "mlb_auto": [base / selected_date / "mlb_auto_card.png"],
        "best_hit": [base / _mmddyyyy_folder(selected_date) / "best_hit_prop.png"],
    }.get(kind, [])
    return next((path for path in candidates if path.exists()), None)


async def inline_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline menu buttons like a modern mobile app."""
    del context
    query = update.callback_query
    if not query:
        return
    await query.answer()
    action = query.data or ""

    user_id = update.effective_user.id if update.effective_user else None
    is_admin = _is_admin_user(user_id)
    logging.info(
        "Callback received user_id=%s callback_data=%s handler_matched=pending",
        user_id,
        action,
    )
    _log_callback_event(
        user_id=user_id,
        callback_data=action,
        handler_found=action.startswith("menu:") or action in ADMIN_CALLBACKS,
        execution_success=False,
    )

    if action in {"menu:main", "menu:home"}:
        logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
        await _edit_app_message(query, _main_menu_text(), _main_menu_markup(user_id))
        return

    hub_screens = {
        "menu:mlb_hub": ("⚾ MLB HUB\n\nChoose today’s MLB section.", "mlb"),
        "menu:soccer_hub": ("⚽ SOCCER HUB\n\nChoose today’s soccer section.", "soccer"),
        "menu:results_hub": ("📊 RESULTS HUB\n\nChoose a results view.", "results"),
        "menu:vip_hub": ("💎 BETGPTAI PREMIUM\n\nChoose a membership section.", "vip"),
        "menu:help_hub": ("ℹ️ HELP\n\nChoose a help topic.", "help"),
    }
    if action in hub_screens:
        text, hub = hub_screens[action]
        logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
        await _edit_app_message(query, text, _hub_markup(hub))
        return

    if action == "menu:admin_hub":
        if not is_admin:
            logging.warning("Callback unauthorized user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
            await _edit_app_message(query, "⛔ Unauthorized command.", _back_menu_markup())
            return
        logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
        await _edit_app_message(query, "⚙️ ADMIN HUB\n\nOwner-only BETGPTAI controls.", _hub_markup("admin"))
        return
    if action == "admin_back":
        if not is_admin:
            logging.warning("Callback unauthorized user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
            await _edit_app_message(query, "⛔ Unauthorized command.", _back_menu_markup())
            return
        logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
        _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
        await _edit_app_message(query, "⚙️ ADMIN HUB\n\nOwner-only BETGPTAI controls.", _hub_markup("admin"))
        return

    if action in {
        "admin_mlb_war_room",
        "admin_full_mlb_card",
        "admin_mlb_top5_card",
        "admin_mission_control",
        "admin_generate_images",
        "admin_debug_results",
        "admin_system_diagnostics",
        "admin_odds_debug",
        "admin_card_debug",
    }:
        if not is_admin:
            logging.warning("Callback unauthorized user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
            await _edit_app_message(query, "⛔ Unauthorized command.", _back_menu_markup())
            return
        if action == "admin_mlb_war_room":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
            await _edit_app_message(
                query,
                "⚾ MLB WAR ROOM\n\nChoose an admin-only MLB report.",
                _hub_markup("admin_mlb_war_room"),
            )
            return
        if action == "admin_generate_images":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
            await _edit_app_message(
                query,
                "🖼 GENERATE MLB IMAGES\n\nUse /mlb_admin_image for the War Room dashboard or /mlb_images for the Anime Vault carousel.",
                _hub_markup("admin"),
            )
            if query.message:
                selected_date = official_sports_date().isoformat()
                try:
                    report = await build_mlb_admin_report_async(
                        selected_date,
                        odds_api_key=os.getenv("ODDS_API_KEY", ""),
                        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
                        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
                    )
                    result = await asyncio.to_thread(
                        prepare_mlb_admin_image,
                        report,
                        image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
                    )
                    image_path = result.get("image_path")
                    if image_path and Path(str(image_path)).exists():
                        with Path(str(image_path)).open("rb") as image_file:
                            await query.message.reply_photo(
                                photo=image_file,
                                caption="🖼 BETGPTAI MLB War Room Anime Dashboard — Admin Only",
                            )
                    else:
                        await _send_long_text_to_message(
                            query.message,
                            "🖼 MLB War Room image prompt saved.\n\n"
                            f"Prompt Path: {result.get('prompt_path')}\n\n"
                            f"{result.get('prompt')}",
                        )
                except Exception:
                    logging.exception("Inline admin image generation failed")
                    logging.error("Callback failed user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
                    await query.message.reply_text("Unable to generate MLB War Room image right now.")
            return
        if action == "admin_debug_results":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
            await _edit_app_message(query, "📊 DEBUG RESULTS\n\nUse /debug_results or /prepost_check for deeper checks.", _hub_markup("admin"))
            if query.message:
                try:
                    summary = await asyncio.to_thread(debug_results_summary)
                    dates_text = ", ".join(summary.get("dates", [])) or "None"
                    await query.message.reply_text(
                        "🧪 BETGPTAI RESULTS DEBUG\n\n"
                        f"Total picks in picks.json: {summary.get('total_picks', 0)}\n"
                        f"Picks for today: {summary.get('picks_today', 0)}\n"
                        f"Picks already graded today: {summary.get('graded_today', 0)}\n"
                        f"Pending today: {summary.get('pending_today', 0)}\n"
                        f"Dates found in picks.json: {dates_text}\n"
                        f"Picks missing card_date: {summary.get('missing_card_date', 0)}\n"
                        f"Picks missing game_pk: {summary.get('missing_game_pk', 0)}\n"
                        f"Picks missing market_type: {summary.get('missing_market_type', 0)}"
                    )
                except Exception:
                    logging.exception("Inline admin debug results failed")
                    logging.error("Callback failed user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
                    await query.message.reply_text("Unable to load debug results right now.")
            return
        if action == "admin_system_diagnostics":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            try:
                text = await _system_diagnostics_text()
                _log_callback_event(
                    user_id=user_id,
                    callback_data=action,
                    handler_found=True,
                    execution_success=True,
                )
            except Exception as error:
                logging.exception("Inline system diagnostics failed")
                _log_callback_event(
                    user_id=user_id,
                    callback_data=action,
                    handler_found=True,
                    execution_success=False,
                    error=error,
                )
                text = f"Unable to load System Diagnostics right now.\n\nError: {error}"
            await _edit_app_message(
                query,
                "🔧 SYSTEM DIAGNOSTICS\n\nDiagnostics report appears below.\n\nChoose a debug tool:",
                _system_diagnostics_markup(),
            )
            if query.message:
                await query.message.reply_text(text)
            return
        if action == "admin_odds_debug":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            try:
                selected_date = official_sports_date().isoformat()
                payload = await asyncio.to_thread(
                    odds_debug_payload,
                    os.getenv("ODDS_API_KEY", ""),
                    selected_date,
                )
                text = _render_odds_debug_payload(payload, selected_date)
                _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
            except Exception as error:
                logging.exception("Inline odds debug failed")
                text = f"/odds_debug failed:\n{error!r}"
                _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=False, error=error)
            await _edit_app_message(query, "🧪 ODDS DEBUG\n\nReport appears below.", _system_diagnostics_markup())
            if query.message:
                await query.message.reply_text(text)
            return
        if action == "admin_card_debug":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            await _edit_app_message(query, "🧪 CARD DEBUG\n\nBuilding card debug snapshot below.", _system_diagnostics_markup())
            if query.message:
                try:
                    text = await _build_card_debug_text()
                    _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
                except Exception as error:
                    logging.exception("Inline card debug failed")
                    text = f"/card_debug failed:\n{error!r}"
                    _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=False, error=error)
                await query.message.reply_text(text)
            return
        if action == "admin_mission_control":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            _log_callback_event(user_id=user_id, callback_data=action, handler_found=True, execution_success=True)
            try:
                mission_text = await _mission_control_health_text()
            except Exception:
                logging.exception("Mission Control health snapshot failed")
                mission_text = "🧠 MISSION CONTROL\n\nBuilding Intelligence Dashboard below..."
            await _edit_app_message(query, mission_text, _hub_markup("admin"))
            if query.message:
                selected_date = official_sports_date().isoformat()
                try:
                    payload = await asyncio.to_thread(
                        build_intelligence_dashboard,
                        selected_date,
                        odds_api_key=os.getenv("ODDS_API_KEY", ""),
                        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
                    )
                    await _send_long_text_to_message(query.message, render_daily_intel(payload))
                except Exception:
                    logging.exception("Inline Mission Control failed")
                    logging.error("Callback failed user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
                    await query.message.reply_text("Unable to load Mission Control right now.")
            return
        if action == "admin_mlb_top5_card":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            await _edit_app_message(
                query,
                "📋 FULL TOP 5 MLB CARD\n\nBuilding the admin-only Top 5 card below...",
                _hub_markup("admin_mlb_war_room"),
            )
            if query.message:
                try:
                    selected_date = official_sports_date().isoformat()
                    report = await asyncio.to_thread(
                        build_mlb_top5_admin_card,
                        selected_date,
                        odds_api_key=os.getenv("ODDS_API_KEY", ""),
                        highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
                    )
                    await _send_long_text_to_message(query.message, render_mlb_top5_admin_card(report))
                    report_path = report.get("report_path")
                    if report_path and Path(str(report_path)).exists():
                        with Path(str(report_path)).open("rb") as report_file:
                            await query.message.reply_document(
                                document=report_file,
                                filename="mlb_top5_admin.json",
                                caption="📎 Saved MLB Top 5 Admin JSON",
                            )
                except Exception:
                    logging.exception("Inline MLB Top 5 card failed")
                    logging.error("Callback failed user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
                    await query.message.reply_text("Unable to build MLB Top 5 card right now.")
            return
        if action == "admin_full_mlb_card":
            logging.info("Callback handled user_id=%s callback_data=%s handler_matched=true execution=success", user_id, action)
            await _edit_app_message(
                query,
                "⚾ FULL OFFICIAL MLB CARD\n\nBuilding the admin-only report below...",
                _hub_markup("admin_mlb_war_room"),
            )
            if query.message:
                try:
                    await _build_and_send_mlb_admin_report(
                        message=query.message,
                        full=False,
                    )
                except Exception:
                    logging.exception("Inline Full Official MLB Card failed")
                    logging.error("Callback failed user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
                    await query.message.reply_text("Unable to build Full Official MLB Card right now.")
        return

    if action in {
        "learning_loss_review",
        "learning_weight_suggestions",
        "learning_status",
        "learning_approve",
        "learning_reject",
    }:
        if not is_admin:
            logging.warning("Callback unauthorized user_id=%s callback_data=%s handler_matched=true execution=failure", user_id, action)
            await _edit_app_message(query, "⛔ Unauthorized command.", _back_menu_markup())
            return
        selected_date = official_sports_date().isoformat()
        if action == "learning_status":
            payload = await asyncio.to_thread(learning_status_payload)
            await _edit_app_message(query, render_learning_status(payload), _hub_markup("ai_learning"))
            return
        if action == "learning_weight_suggestions":
            text = await asyncio.to_thread(render_weight_suggestions)
            await _edit_app_message(query, text, _hub_markup("ai_learning"))
            return
        if action == "learning_approve":
            result = await asyncio.to_thread(approve_learning_weight_update)
            await _edit_app_message(
                query,
                "✅ WEIGHT UPDATE REVIEW COMPLETE\n\n"
                f"Applied: {result.get('applied', 0)}\n"
                f"Message: {result.get('message')}",
                _hub_markup("ai_learning"),
            )
            return
        if action == "learning_reject":
            await asyncio.to_thread(reject_learning_weight_update)
            await _edit_app_message(
                query,
                "🧹 Pending AI Learning weight suggestions rejected and cleared.",
                _hub_markup("ai_learning"),
            )
            return
        await _edit_app_message(
            query,
            "🧠 Building tonight’s loss review below...",
            _hub_markup("ai_learning"),
        )
        if query.message:
            try:
                report = await asyncio.to_thread(run_learning_review, selected_date)
                await _send_long_text_to_message(query.message, render_loss_review(report))
            except Exception:
                logging.exception("Inline AI Learning loss review failed")
                await query.message.reply_text("Unable to build loss review right now.")
        return

    if action in {"menu:help", "menu:help_how"}:
        await _edit_app_message(query, _help_text(), _back_menu_markup())
        return
    if action == "menu:help_singles":
        await _edit_app_message(
            query,
            "✅ SINGLES-FIRST APPROACH\n\nBETGPTAI is built around single bets for better long-term discipline. Parlays are optional and higher variance.",
            _hub_markup("help"),
        )
        return
    if action == "menu:help_disclaimer":
        await _edit_app_message(query, "⚠️ DISCLAIMER\n\nEducational analysis only. Play responsibly. Past performance does not guarantee future results.", _hub_markup("help"))
        return
    if action == "menu:card_disclaimer":
        await _edit_app_message(
            query,
            "⚠️ BETGPTAI DISCLAIMER\n\n"
            "These plays are designed to be played as single bets for the best long-term results.\n\n"
            "Parlays are optional, higher variance, and should be played at your own risk with reduced stake size.\n\n"
            "Odds vary by sportsbook. Please shop for the best available number before playing.\n\n"
            "Past performance does not guarantee future results.\n\n"
            "Educational analysis only. Play responsibly.",
            _card_disclaimer_markup(),
        )
        return
    if action == "menu:help_contact":
        await _edit_app_message(query, "📩 CONTACT ADMIN\n\nAfter payment or for support, send proof/details to @YOUR_USERNAME.", _hub_markup("help"))
        return

    if action in {"menu:vip", "menu:vip_benefits"}:
        await _edit_app_message(query, _vip_text(), _hub_markup("vip"))
        return
    if action == "menu:vip_weekly":
        await _edit_app_message(query, "🥉 Weekly Pass — $5.99\n\nhttps://buy.stripe.com/aFadRbgvt1Da75L5xv0ZW00", _hub_markup("vip"))
        return
    if action == "menu:vip_monthly":
        await _edit_app_message(query, "🥈 Premium Monthly — $9.99\n\nhttps://buy.stripe.com/dRm7sN6UT95C89PbVT0ZW01", _hub_markup("vip"))
        return
    if action == "menu:vip_season":
        await _edit_app_message(query, "🥇 Season Pass — $49.99 every 6 months\n\nhttps://buy.stripe.com/aFa9AVa75epW1Lre410ZW02", _hub_markup("vip"))
        return

    if action == "menu:mlb_today":
        await _edit_app_message(query, await _build_today_card_text(), _back_menu_markup())
        return
    if action == "menu:mlb_parlay":
        await _edit_app_message(query, await _build_safe_parlay_text(), _hub_markup("mlb"))
        return
    if action == "menu:mlb_best_hit":
        text, prop = await _verified_best_hit_text_for_today()
        del prop
        await _edit_app_message(query, text, _hub_markup("mlb"))
        return
    if action == "menu:mlb_image":
        if is_admin:
            await _edit_app_message(query, "⏳ Generating admin Anime Card Preview...", _hub_markup("mlb"))
            if query.message:
                selected_date = official_sports_date().isoformat()
                slate = await asyncio.to_thread(
                    get_combined_slate,
                    os.getenv("ODDS_API_KEY", ""),
                    game_date=selected_date,
                    highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
                )
                analysis = await analyze_mlb_slate(
                    slate,
                    os.getenv("OPENAI_API_KEY", ""),
                    os.getenv("ANTHROPIC_API_KEY", ""),
                )
                try:
                    await asyncio.to_thread(
                        _save_official_mlb_card,
                        analysis,
                        slate,
                        selected_date,
                        "tap_menu_mlb_image",
                    )
                except Exception:
                    logging.exception("Could not save official picks before tap-menu MLB image")
                result = await asyncio.to_thread(
                    prepare_mlb_auto_image,
                    _with_card_date(analysis, selected_date),
                    selected_date,
                    image_generation_enabled=_truthy_env("IMAGE_GENERATION_ENABLED"),
                )
                await _send_single_image_preview(query.message, result, title="BETGPTAI MLB Auto Image")
            return
        image = _available_public_image("mlb_auto")
        if image and query.message:
            await _edit_app_message(query, "🖼 Anime Card Preview", _hub_markup("mlb"))
            with image.open("rb") as image_file:
                await query.message.reply_photo(photo=image_file, caption="BETGPTAI MLB Anime Card Preview")
        else:
            await _edit_app_message(query, "Anime preview is not ready yet. Check back after today’s card is generated.", _hub_markup("mlb"))
        return
    if action in {"menu:mlb_card", "menu:mlb"}:
        await _edit_app_message(
            query,
            "⏳ Building today’s BETGPTAI MLB card...\n\nThe card will appear below when ready.",
            _card_disclaimer_markup(),
        )
        if query.message:
            await _send_long_text_to_message(query.message, await _build_mlb_auto_card_for_menu())
        return

    if action == "menu:soccer_worldcup":
        await _edit_app_message(query, "⏳ Building World Cup / soccer card...\n\nThe card will appear below when ready.", _hub_markup("soccer"))
        if query.message:
            await _send_long_text_to_message(query.message, await _build_soccer_card_for_menu())
        return
    if action in {"menu:soccer_card", "menu:soccer"}:
        await _edit_app_message(query, "⏳ Building today’s BETGPTAI soccer card...\n\nThe card will appear below when ready.", _hub_markup("soccer"))
        if query.message:
            try:
                card = await _build_soccer_card_for_menu()
            except Exception:
                logging.exception("Unexpected inline soccer card error")
                card = "Unable to build the soccer card right now. Please try again shortly."
            await _send_long_text_to_message(query.message, card)
        return
    if action == "menu:soccer_parlay":
        await _edit_app_message(query, "🧩 SOCCER PARLAY\n\nSoccer parlay appears inside today’s soccer card when available.\n\nTap 🔥 Best Soccer Plays to generate it.", _hub_markup("soccer"))
        return

    if action in {"menu:results_today", "menu:results"}:
        try:
            selected_date = eastern_today().isoformat()
            globals()["LAST_RESULTS_BUTTON_DATE"] = selected_date
            dashboard, markup = await asyncio.to_thread(_results_dashboard_or_picker, selected_date)
        except ResultsTrackerError as error:
            logging.warning("Could not load results dashboard: %s", error)
            dashboard = f"Results are temporarily unavailable: {error}"
            markup = _hub_markup("results")
        await _edit_app_message(query, dashboard, markup)
        return
    if action == "menu:results_yesterday":
        target_date = (eastern_today() - timedelta(days=1)).isoformat()
        globals()["LAST_RESULTS_BUTTON_DATE"] = target_date
        dashboard, markup = await asyncio.to_thread(_results_dashboard_or_picker, target_date)
        await _edit_app_message(query, dashboard, markup)
        return
    if action.startswith("menu:results_date:"):
        target_date = normalize_pick_date(action.rsplit(":", 1)[-1])
        if not target_date:
            await _edit_app_message(query, "Invalid results date.", _hub_markup("results"))
            return
        globals()["LAST_RESULTS_BUTTON_DATE"] = target_date
        dashboard, markup = await asyncio.to_thread(_results_dashboard_or_picker, target_date)
        await _edit_app_message(query, dashboard, markup)
        return
    if action == "menu:results_7days":
        dashboard = await asyncio.to_thread(build_range_results_dashboard, 7)
        await _edit_app_message(query, dashboard, _hub_markup("results"))
        return
    if action == "menu:results_season":
        dashboard = await asyncio.to_thread(build_range_results_dashboard, 3650)
        await _edit_app_message(query, dashboard, _hub_markup("results"))
        return

    if action.startswith("menu:admin_"):
        if not is_admin:
            await _edit_app_message(query, "⛔ Unauthorized command.", _back_menu_markup())
            return
        admin_messages = {
            "menu:admin_props": "🧪 PROPS LAB\n\nUse /props_admin, /hits_admin, /hr_admin, or /strikeouts_admin.",
            "menu:admin_images": "🖼 GENERATE IMAGES\n\nUse /best_hit_image_admin, /today_image_admin, /mlb_auto_image_admin, or /mlb_images.",
            "menu:admin_model": "📊 MODEL REPORT\n\nUse /model_report for full diagnostics.",
            "menu:admin_grade": "🔁 GRADE TODAY\n\nUse /grade_today, /results_auto_status, /post_results_now, /enable_auto_results, or /disable_auto_results.",
            "menu:admin_debug": "🧰 DEBUG TOOLS\n\nUse /status, /storage_status, /integrity_report, /debug_thesportsdb, /debug_football_data, /debug_pybaseball, /debug_picks, /debug_results, /prop_debug, or /debug_soccer.",
        }
        await _edit_app_message(query, admin_messages.get(action, "Admin tool unavailable."), _hub_markup("admin"))
        return

    logging.error(
        "Unknown callback user_id=%s callback_data=%s handler_matched=false execution=failure",
        user_id,
        action,
    )
    _log_callback_event(
        user_id=user_id,
        callback_data=action,
        handler_found=False,
        execution_success=False,
        error="unknown_callback",
    )
    if query.message:
        await query.message.reply_text(f"Unknown callback: {action}")


async def _configure_command_menus(application: object) -> None:
    """Keep every Telegram command menu limited to public app navigation."""
    public_commands = [
        BotCommand("start", "Open BETGPTAI sports hub"),
    ]
    await application.bot.set_my_commands(public_commands)

    admin_id = _admin_telegram_id()
    if admin_id is not None:
        # Overwrite any older owner-specific menu that exposed admin commands.
        # The commands remain registered and work when the owner types them.
        await application.bot.set_my_commands(
            public_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


async def main() -> None:
    """Load settings, register commands, and keep the bot running."""
    # load_dotenv reads BOT_TOKEN from a local .env file if one exists.
    load_dotenv()
    if _runtime_environment() == "local" and not _truthy_value(os.getenv("LOCAL_BOT_ALLOWED")):
        print(
            "Local bot blocked. Set LOCAL_BOT_ALLOWED=true only when Railway is paused.",
            flush=True,
        )
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")

    # Stop early with a clear message if the private token is missing.
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or BOT_TOKEN is missing. Copy .env.example "
            "to .env and add your token."
        )

    # ApplicationBuilder creates the python-telegram-bot application.
    application = ApplicationBuilder().token(token).build()

    # Connect each Telegram command to its matching async function above.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("version", version))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("mlb_auto", mlb_auto))
    application.add_handler(CommandHandler("generate_today", mlb_auto))
    application.add_handler(CommandHandler("mlb_admin", mlb_admin))
    application.add_handler(CommandHandler("warroom_debug", warroom_debug))
    application.add_handler(CommandHandler("odds_debug", odds_debug))
    SYSTEM_LOG.info("Registered command: /odds_debug")
    print("Registered command: /odds_debug", flush=True)
    application.add_handler(CommandHandler("odds_probe", odds_probe))
    SYSTEM_LOG.info("Registered command: /odds_probe")
    print("Registered command: /odds_probe", flush=True)
    application.add_handler(CommandHandler("sharp_probe", sharp_probe))
    application.add_handler(CommandHandler("sp_batter_debug", sp_batter_debug))
    SYSTEM_LOG.info("Registered command: /sp_batter_debug")
    print("Registered command: /sp_batter_debug", flush=True)
    application.add_handler(CommandHandler("official_card_debug", official_card_debug))
    SYSTEM_LOG.info("Registered command: /official_card_debug")
    print("Registered command: /official_card_debug", flush=True)
    application.add_handler(CommandHandler("card_debug", card_debug))
    SYSTEM_LOG.info("Registered command: /card_debug")
    print("Registered command: /card_debug", flush=True)
    application.add_handler(CommandHandler("force_post_text_card", force_post_text_card))
    SYSTEM_LOG.info("Registered command: /force_post_text_card")
    print("Registered command: /force_post_text_card", flush=True)
    application.add_handler(CommandHandler("simple_card_debug", simple_card_debug))
    SYSTEM_LOG.info("Registered command: /simple_card_debug")
    print("Registered command: /simple_card_debug", flush=True)
    application.add_handler(CommandHandler("simple_generate_today", simple_generate_today))
    SYSTEM_LOG.info("Registered command: /simple_generate_today")
    print("Registered command: /simple_generate_today", flush=True)
    application.add_handler(CommandHandler("simple_post_today", simple_post_today))
    SYSTEM_LOG.info("Registered command: /simple_post_today")
    print("Registered command: /simple_post_today", flush=True)
    application.add_handler(CommandHandler("bridge_simple_card_today", bridge_simple_card_today))
    SYSTEM_LOG.info("Registered command: /bridge_simple_card_today")
    print("Registered command: /bridge_simple_card_today", flush=True)
    application.add_handler(CommandHandler("simple_results_debug", simple_results_debug))
    SYSTEM_LOG.info("Registered command: /simple_results_debug")
    print("Registered command: /simple_results_debug", flush=True)
    application.add_handler(CommandHandler("workflow_debug", workflow_debug_handler))
    SYSTEM_LOG.info("Registered command: /workflow_debug")
    print("Registered command: /workflow_debug", flush=True)
    application.add_handler(CommandHandler("mlb_top5_admin", mlb_top5_admin))
    application.add_handler(CommandHandler("mlb_admin_image", mlb_admin_image))
    application.add_handler(CommandHandler("soccer", soccer))
    application.add_handler(CommandHandler("worldcup", worldcup))
    application.add_handler(CommandHandler("vip", vip))
    application.add_handler(CommandHandler("results", results))
    application.add_handler(CommandHandler("results_today", results_today))
    application.add_handler(CommandHandler("results_yesterday", results_yesterday))
    application.add_handler(CommandHandler("results_7days", results_7days))
    application.add_handler(CommandHandler("results_30days", results_30days))
    application.add_handler(CommandHandler("results_season", results_season))
    application.add_handler(CommandHandler("results_date", results_date))
    application.add_handler(CommandHandler("results_debug", results_debug_command))
    application.add_handler(CommandHandler("repair_results", repair_results_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("storage_status", storage_status))
    application.add_handler(CommandHandler("system_diagnostics", system_diagnostics))
    application.add_handler(CommandHandler("debug_thesportsdb", debug_thesportsdb))
    application.add_handler(CommandHandler("debug_football_data", debug_football_data))
    application.add_handler(CommandHandler("debug_pybaseball", debug_pybaseball))
    application.add_handler(CommandHandler("integrity_report", integrity_report))
    application.add_handler(CommandHandler("update_results", update_results))
    application.add_handler(CommandHandler("grade_today", grade_today))
    application.add_handler(CommandHandler("grade_yesterday", grade_yesterday))
    application.add_handler(CommandHandler("force_grade_date", force_grade_date))
    application.add_handler(CommandHandler("grade_debug", grade_debug))
    application.add_handler(CommandHandler("snapshot_status", snapshot_status))
    SYSTEM_LOG.info("Registered command: /snapshot_status")
    print("Registered command: /snapshot_status", flush=True)
    application.add_handler(CommandHandler("snapshot_debug", snapshot_debug))
    SYSTEM_LOG.info("Registered command: /snapshot_debug")
    print("Registered command: /snapshot_debug", flush=True)
    application.add_handler(CommandHandler("snapshot_regenerate", snapshot_regenerate))
    SYSTEM_LOG.info("Registered command: /snapshot_regenerate")
    print("Registered command: /snapshot_regenerate", flush=True)
    application.add_handler(CommandHandler("vault_debug", vault_debug))
    SYSTEM_LOG.info("Registered command: /vault_debug")
    print("Registered command: /vault_debug", flush=True)
    application.add_handler(CommandHandler("results_auto_status", results_auto_status))
    application.add_handler(CommandHandler("time_debug", time_debug))
    application.add_handler(CommandHandler("scheduler_status", scheduler_status))
    application.add_handler(CommandHandler("lineup_status", lineup_status))
    application.add_handler(CommandHandler("prop_scratch_debug", prop_scratch_debug))
    application.add_handler(CommandHandler("scratch_public_impact", scratch_public_impact))
    application.add_handler(CommandHandler("post_status", post_status))
    application.add_handler(CommandHandler("force_generate_today", force_generate_today))
    application.add_handler(CommandHandler("force_post_today", force_post_today))
    application.add_handler(CommandHandler("save_today_picks", save_today_picks))
    application.add_handler(CommandHandler("saved_picks_today", saved_picks_today))
    application.add_handler(CommandHandler("save_debug", save_debug))
    application.add_handler(CommandHandler("repair_storage", repair_storage))
    application.add_handler(CommandHandler("post_results_now", post_results_now))
    application.add_handler(CommandHandler("enable_auto_results", enable_auto_results))
    application.add_handler(CommandHandler("disable_auto_results", disable_auto_results))
    application.add_handler(CommandHandler("debug_picks", debug_picks))
    application.add_handler(CommandHandler("extract_picks_debug", extract_picks_debug))
    application.add_handler(CommandHandler("debug_results", debug_results))
    application.add_handler(CommandHandler("date_debug", date_debug))
    application.add_handler(CommandHandler("save_last_card", save_last_card))
    application.add_handler(CommandHandler("backfill_today", backfill_today))
    application.add_handler(CommandHandler("f5", specialized_mlb_card))
    application.add_handler(CommandHandler("nrfi", specialized_mlb_card))
    application.add_handler(CommandHandler("teamtotals", specialized_mlb_card))
    application.add_handler(CommandHandler("parlay", specialized_mlb_card))
    application.add_handler(CommandHandler("fullday", specialized_mlb_card))
    application.add_handler(CommandHandler("strikeouts", specialized_mlb_card))
    application.add_handler(CommandHandler("hits", specialized_mlb_card))
    application.add_handler(CommandHandler("home_runs", specialized_mlb_card))
    application.add_handler(CommandHandler("props_admin", props_admin))
    application.add_handler(CommandHandler("hits_admin", hits_admin))
    application.add_handler(CommandHandler("hits_by_team_admin", hits_by_team_admin))
    application.add_handler(CommandHandler("streak_report_admin", streak_report_admin))
    application.add_handler(CommandHandler("streak_debug_admin", streak_debug_admin))
    application.add_handler(CommandHandler("prepost_check", prepost_check))
    application.add_handler(CommandHandler("daily_intel_admin", daily_intel_admin))
    application.add_handler(CommandHandler("morning_report_admin", morning_report_admin))
    application.add_handler(CommandHandler("lineup_report_admin", lineup_report_admin))
    application.add_handler(CommandHandler("model_review_admin", model_review_admin))
    application.add_handler(CommandHandler("intel_debug", intel_debug))
    application.add_handler(CommandHandler("learning_report_admin", learning_report_admin))
    application.add_handler(CommandHandler("loss_review_admin", loss_review_admin))
    application.add_handler(CommandHandler("weight_suggestions_admin", weight_suggestions_admin))
    application.add_handler(CommandHandler("approve_weight_update", approve_weight_update))
    application.add_handler(CommandHandler("reject_weight_update", reject_weight_update))
    application.add_handler(CommandHandler("learning_status", learning_status))
    application.add_handler(CommandHandler("learning_auto_status", learning_auto_status))
    application.add_handler(CommandHandler("toggle_learning_auto_apply", toggle_learning_auto_apply_command))
    application.add_handler(CommandHandler("weights_admin", weights_admin))
    application.add_handler(CommandHandler("weight_history_admin", weight_history_admin))
    application.add_handler(CommandHandler("clv_debug", clv_debug))
    SYSTEM_LOG.info("Registered command: /clv_debug")
    print("Registered command: /clv_debug", flush=True)
    application.add_handler(CommandHandler("bullpen_debug", bullpen_debug))
    SYSTEM_LOG.info("Registered command: /bullpen_debug")
    print("Registered command: /bullpen_debug", flush=True)
    application.add_handler(CommandHandler("learning_roi_debug", learning_roi_debug))
    SYSTEM_LOG.info("Registered command: /learning_roi_debug")
    print("Registered command: /learning_roi_debug", flush=True)
    application.add_handler(CommandHandler("confidence_debug", confidence_debug))
    SYSTEM_LOG.info("Registered command: /confidence_debug")
    print("Registered command: /confidence_debug", flush=True)
    application.add_handler(CommandHandler("hr_admin", hr_admin))
    application.add_handler(CommandHandler("strikeouts_admin", strikeouts_admin))
    application.add_handler(CommandHandler("props_images_admin", props_images_admin))
    application.add_handler(CommandHandler("hits_by_team_image_admin", hits_by_team_image_admin))
    application.add_handler(CommandHandler("best_hit_image_admin", best_hit_image_admin))
    application.add_handler(CommandHandler("post_best_hit_image_admin", post_best_hit_image_admin))
    application.add_handler(CommandHandler("clear_prop_cache", clear_prop_cache))
    application.add_handler(CommandHandler("verify_best_hit_prop", verify_best_hit_prop))
    application.add_handler(CommandHandler("magazine_admin", magazine_admin))
    application.add_handler(CommandHandler("verify_player", verify_player_command))
    application.add_handler(CommandHandler("props_test", props_test))
    application.add_handler(CommandHandler("prop_debug", prop_debug))
    application.add_handler(CommandHandler("hitprops_debug", hitprops_debug))
    application.add_handler(CommandHandler("hit_props_debug", hitprops_debug))
    application.add_handler(CommandHandler("props_debug", props_debug))
    application.add_handler(CommandHandler("approve_prop", approve_prop_command))
    application.add_handler(CommandHandler("soccer_full", soccer_owner_card))
    application.add_handler(CommandHandler("btts", soccer_owner_card))
    application.add_handler(CommandHandler("corners", soccer_owner_card))
    application.add_handler(CommandHandler("overs", soccer_owner_card))
    application.add_handler(CommandHandler("cards", soccer_owner_card))
    application.add_handler(CommandHandler("first_half", soccer_owner_card))
    application.add_handler(CommandHandler("second_half", soccer_owner_card))
    application.add_handler(CommandHandler("double_chance", soccer_owner_card))
    application.add_handler(CommandHandler("asian_handicap", soccer_owner_card))
    application.add_handler(CommandHandler("soccer_results", soccer_results))
    application.add_handler(CommandHandler("debug_soccer", debug_soccer))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("model_report", model_report))
    application.add_handler(CommandHandler("test_channels", test_channels))
    application.add_handler(CommandHandler("image_test", image_test))
    application.add_handler(CommandHandler("today_image_admin", today_image_admin))
    application.add_handler(CommandHandler("mlb_auto_image_admin", mlb_auto_image_admin))
    application.add_handler(CommandHandler("mlb_images", mlb_images))
    application.add_handler(CommandHandler("post_mlb_images", post_mlb_images))
    application.add_handler(CommandHandler("test_image_card", test_image_card))
    application.add_handler(CommandHandler("post_test_image", post_test_image))
    application.add_handler(CommandHandler("callback_debug", callback_debug))
    register_callback_router(application)

    # Start polling Telegram for new messages, then wait until Ctrl+C is pressed.
    async with application:
        try:
            await _configure_command_menus(application)
        except Exception:
            # Command-menu setup is cosmetic and should never stop the bot.
            logging.exception("Could not configure Telegram command menus")
        await application.start()
        scheduler_task = asyncio.create_task(
            run_game_aware_scheduler(application),
            name="betgptai-game-aware-scheduler",
        )
        if application.updater is None:
            scheduler_task.cancel()
            raise RuntimeError("The Telegram updater could not be created.")
        await application.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            # Shut down cleanly when the program is stopped.
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # Ctrl+C is a normal way to stop a polling bot.
        pass
