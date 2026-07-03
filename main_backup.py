"""A small, beginner-friendly Telegram bot for sharing baseball plays."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat, ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_analysis import (
    analyze_mlb_slate,
    analyze_specialized_mlb_slate,
    build_fallback_card,
    get_last_analysis_metadata,
)
from card_format import PARLAY_NOTE, RECOMMENDATION_FOOTER, TIMED_CARD_FOOTER
from card_time import eastern_now, official_sports_date, tomorrow_sports_date
from game_time import mlb_game_block
from image_card_generator import generate_test_mlb_card, short_caption
from mlb_data import MLBDataError, get_combined_slate
from model_report import load_model_report, save_model_report
from api_sports_baseball import api_sports_baseball_available
from fangraphs_data import fangraphs_available
from savant_data import savant_available
from soccer_analysis import analyze_soccer_slate, soccer_debug_report
from soccer_master_engines import soccer_slate_summary
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
from results_tracker import (
    ResultsTrackerError,
    debug_picks_summary,
    grade_mlb_picks_for_date,
    get_most_recent_featured_picks,
    load_picks,
    load_results,
    save_official_picks,
    save_soccer_picks,
    update_results_from_mlb,
)
from posting_scheduler import run_game_aware_scheduler


# Show useful information in the terminal while the bot is running.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def _admin_telegram_id() -> int | None:
    """Read the single authorized Telegram user ID from the environment."""
    try:
        return int(os.getenv("MY_TELEGRAM_ID", "").strip())
    except ValueError:
        logging.error("MY_TELEGRAM_ID must be a numeric Telegram user ID")
        return None


async def _require_admin(update: Update) -> bool:
    """Reject private commands unless they come from the configured admin."""
    configured_id = _admin_telegram_id()
    requesting_id = update.effective_user.id if update.effective_user else None
    if configured_id is not None and requesting_id == configured_id:
        return True
    if update.message:
        await update.message.reply_text("⛔ Unauthorized command.")
    return False


def _database_counts() -> dict[str, int]:
    """Count tracked, pending, and settled picks for the admin panel."""
    picks = load_picks()
    pending = sum(pick.get("result") == "pending" for pick in picks)
    graded = sum(
        pick.get("result") in {"win", "loss", "push"} for pick in picks
    )
    return {"total": len(picks), "pending": pending, "graded": graded}


def _public_keyboard() -> ReplyKeyboardMarkup:
    """Return the simple app-style keyboard shown on /start and /help."""
    return ReplyKeyboardMarkup(
        [
            ["🎯 Today’s Picks", "⚾ MLB Card"],
            ["⚽ Soccer Card", "📊 Results"],
            ["💎 Premium", "ℹ️ Help"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose a BETGPTAI section",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the clean public BETGPTAI sports hub."""
    del context
    if not update.message:
        return
    await update.message.reply_text(
        "⚾🏈🏀⚽ BETGPTAI SPORTS HUB\n\n"
        "AI-Powered Sports Analysis\n\n"
        "━━━━━━━━━━━━\n\n"
        "🔥 TODAY’S FEATURED PICKS\n"
        "🎯 Type /today\n"
        "📅 Type /tomorrow for next-day lines\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚾ MLB CARD\n"
        "Today’s free MLB analysis\n"
        "Type /mlb_auto\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚽ SOCCER CARD\n"
        "Today’s free soccer analysis\n"
        "Type /soccer\n\n"
        "━━━━━━━━━━━━\n\n"
        "📊 RESULTS TRACKER\n"
        "Type /results\n\n"
        "━━━━━━━━━━━━\n\n"
        "💎 PREMIUM MEMBERSHIP\n"
        "Type /vip\n\n"
        "━━━━━━━━━━━━\n\n"
        "📋 HELP MENU\n"
        "Type /help\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚠️ BETGPTAI PHILOSOPHY\n\n"
        "✅ Pregame only\n"
        "✅ Simplicity\n"
        "✅ Stability\n"
        "✅ Singles-first approach\n"
        "📈 Long-Term Profitability\n"
        "🧩 Parlays Optional\n\n"
        "Educational analysis only. Play responsibly.",
        reply_markup=_public_keyboard(),
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the short public command guide and persistent navigation keyboard."""
    del context
    if not update.message:
        return
    await update.message.reply_text(
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
        "Educational analysis only. Play responsibly.",
        reply_markup=_public_keyboard(),
    )


def _format_line(value: object) -> str:
    """Format saved American odds without exposing a sportsbook name."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "Unavailable"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"+{value}" if value > 0 else str(value)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show only the featured play and safe parlay from the latest saved card."""
    del context
    if not update.message:
        return

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
        await update.message.reply_text(
            "Today’s picks are still being prepared. "
            "Type /mlb_auto to generate today’s card."
        )
        return

    risk_grade = featured.get("risk_grade")
    risk_text = (
        f"{risk_grade}/10"
        if isinstance(risk_grade, (int, float)) and not isinstance(risk_grade, bool)
        else "Unavailable"
    )
    play_game = featured.get("play_game", {})
    play_context = (
        mlb_game_block(play_game)
        if isinstance(play_game, dict) and play_game.get("away_team")
        else "🕒 Time unavailable ET"
    )
    leg_details = featured.get("parlay_leg_details", [])

    def parlay_leg(index: int) -> str:
        """Format a saved parlay leg with its original matchup and ET time."""
        detail = (
            leg_details[index]
            if isinstance(leg_details, list)
            and index < len(leg_details)
            and isinstance(leg_details[index], dict)
            else {}
        )
        context_text = (
            mlb_game_block(detail)
            if detail.get("away_team")
            else "🕒 Time unavailable ET"
        )
        return f"✅ {legs[index]}\n{context_text}"

    await update.message.reply_text(
        "🎯 BETGPTAI TODAY\n\n"
        "━━━━━━━━━━━━\n\n"
        "🔥 PLAY OF THE DAY\n\n"
        f"⚾ {play}\n"
        f"{play_context}\n"
        f"🎯 Confidence Grade: {risk_text}\n\n"
        "━━━━━━━━━━━━\n\n"
        "🧩 SAFE PARLAY OF THE DAY\n\n"
        f"{parlay_leg(0)}\n\n"
        f"{parlay_leg(1)}\n\n"
        f"{PARLAY_NOTE}\n\n"
        "━━━━━━━━━━━━\n\n"
        "📋 Want the full free MLB card?\n"
        "Type /mlb_auto\n\n"
        "💎 Want premium full slate?\n"
        "Type /vip\n\n"
        f"{TIMED_CARD_FOOTER}"
    )


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
        await update.message.reply_text(chunk)


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

    try:
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

        # Save the exact free card immediately after generation and before the
        # card is sent. This guarantees that every delivered pick is tracked.
        try:
            saved_count = await asyncio.to_thread(
                save_official_picks, analysis, slate, selected_date
            )
            print(
                f"Saved {saved_count} official picks to picks.json",
                flush=True,
            )
        except Exception:
            logging.exception("Could not save official picks to picks.json")
            await update.message.reply_text(
                "The card was generated, but its picks could not be saved. "
                "No picks were sent. Check picks.json and try again."
            )
            return

        # Delivery happens only after picks.json has been updated successfully.
        await _send_long_message(update, analysis)
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
            "💎 BETGPTAI PREMIUM MEMBERSHIP\n\n"
            "Unlock:\n\n"
            "⚾ Full MLB Cards\n"
            "⚾ F5 Plays\n"
            "⚾ Team Totals\n"
            "⚾ NRFI Leans\n"
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
            sports_db_api_key=os.getenv("THE_SPORTS_DB_API_KEY", ""),
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
        await _send_long_message(update, card)
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
        sports_db_api_key=os.getenv("THE_SPORTS_DB_API_KEY", ""),
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
                save_official_picks, mlb_card, mlb_slate, target_date
            )
            print(f"Saved {saved_count} official picks to picks.json", flush=True)
        except Exception:
            logging.exception("Could not save tomorrow's MLB picks")
        await _send_long_message(update, mlb_card)
    if _slate_has_lines(soccer_slate):
        soccer_card = await analyze_soccer_slate(
            soccer_slate,
            os.getenv("OPENAI_API_KEY", ""),
            "public",
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
        await _send_long_message(update, soccer_card)


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
            sports_db_api_key=os.getenv("THE_SPORTS_DB_API_KEY", ""),
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
    """Display the professional BETGPTAI results dashboard."""
    del context
    if not update.message:
        return

    try:
        dashboard = await asyncio.to_thread(load_results)
    except ResultsTrackerError as error:
        logging.warning("Could not load results dashboard: %s", error)
        await update.message.reply_text(
            f"Results are temporarily unavailable: {error}"
        )
        return

    def record(period: str) -> str:
        """Format one dashboard period without duplicating message code."""
        data = dashboard.get(period, {})
        return (
            f"W-L-P: {data.get('wins', 0)}-{data.get('losses', 0)}-"
            f"{data.get('pushes', 0)}\n"
            f"Win %: {data.get('win_percentage', 0):g}%\n"
            f"Profit Units: {data.get('profit_units', 0):+g}"
        )

    await update.message.reply_text(
        "📊 BETGPTAI RESULTS TRACKER\n\n"
        f"Overall:\n{record('overall')}\n\n"
        f"Moneyline:\n{record('moneyline')}\n\n"
        f"F5 Moneyline:\n{record('f5_moneyline')}\n\n"
        f"Runline:\n{record('runline')}\n\n"
        f"Full Game Totals:\n{record('totals')}\n\n"
        f"Team Totals:\n{record('team_totals')}\n\n"
        f"Parlays:\n{record('parlays')}\n\n"
        f"Last 7 Days:\n{record('last_7_days')}\n\n"
        f"Last 30 Days:\n{record('last_30_days')}\n\n"
        f"Season:\n{record('season')}\n\n"
        f"Last Updated: {dashboard.get('last_updated', 'Unavailable')}\n\n"
        f"{RECOMMENDATION_FOOTER}"
    )


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
    selected_date = official_sports_date().isoformat()
    await update.message.reply_text("Checking today’s saved MLB picks... ⚾")
    try:
        summary = await asyncio.to_thread(grade_mlb_picks_for_date, selected_date)
    except Exception:
        logging.exception("Unexpected /grade_today error")
        await update.message.reply_text(
            "Unable to grade today’s picks right now. Check the terminal and try again."
        )
        return
    await update.message.reply_text(
        "✅ RESULTS UPDATE COMPLETE\n\n"
        f"Newly Graded: {summary.get('newly_graded', 0)}\n"
        f"Still Pending: {summary.get('pending', 0)}\n"
        f"Missing Metadata: {summary.get('missing_metadata', 0)}\n"
        f"Errors: {summary.get('errors', 0)}"
    )


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
        f"Picks missing selected_team: {summary.get('missing_selected_team', 0)}\n\n"
        "Last grading errors:\n"
        f"{error_text}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show private configuration status without revealing secret values."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return

    def configured(name: str) -> str:
        return "✅ Available" if os.getenv(name, "").strip() else "❌ Missing"

    try:
        counts = await asyncio.to_thread(_database_counts)
    except ResultsTrackerError:
        logging.exception("Could not count picks for /status")
        counts = {"total": 0}

    try:
        await asyncio.to_thread(load_results)
        database_status = "✅ Healthy"
    except ResultsTrackerError:
        logging.exception("Could not read results database for /status")
        database_status = "❌ Unavailable"

    (
        sportsdb_available,
        baseball_savant_available,
        fangraphs_ok,
        clubelo_available,
        understat_available,
        api_football_ok,
        api_sports_baseball_ok,
    ) = await asyncio.gather(
        asyncio.to_thread(
            check_thesportsdb, os.getenv("THE_SPORTS_DB_API_KEY", "")
        ),
        asyncio.to_thread(savant_available),
        asyncio.to_thread(fangraphs_available),
        asyncio.to_thread(check_clubelo),
        asyncio.to_thread(check_understat),
        asyncio.to_thread(api_football_available, os.getenv("API_FOOTBALL_KEY", "")),
        asyncio.to_thread(
            api_sports_baseball_available,
            os.getenv("API_SPORTS_KEY", "") or os.getenv("API_FOOTBALL_KEY", ""),
        ),
    )
    sportsdb_status = "✅ Available" if sportsdb_available else "❌ Unavailable"
    savant_status = (
        "✅ Available" if baseball_savant_available else "❌ Unavailable"
    )
    fangraphs_status = "✅ Available" if fangraphs_ok else "❌ Unavailable"
    clubelo_status = "✅ Available" if clubelo_available else "❌ Unavailable"
    understat_status = "✅ Available" if understat_available else "❌ Unavailable"
    api_football_status = "✅ Available" if api_football_ok else "❌ Unavailable"
    api_sports_baseball_status = (
        "✅ Available" if api_sports_baseball_ok else "❌ Unavailable"
    )
    serpapi_status = (
        "✅ Configured"
        if check_serpapi(os.getenv("SERPAPI_KEY", ""))
        else "➖ Not configured"
    )

    await update.message.reply_text(
        "🕊 BETGPTAI API STATUS\n\n"
        "Telegram Bot: ✅ Online\n"
        "MLB Stats: ✅ Available\n"
        f"Baseball Savant: {savant_status}\n"
        f"Odds API: {configured('ODDS_API_KEY')}\n"
        f"OpenAI: {configured('OPENAI_API_KEY')}\n"
        f"Claude: {configured('ANTHROPIC_API_KEY')}\n"
        f"Highlightly: {configured('HIGHLIGHTLY_API_KEY')}\n"
        f"API-Sports Baseball: {api_sports_baseball_status}\n"
        f"Optional API-Football: {api_football_status}\n"
        f"Football-Data.org: {configured('FOOTBALL_DATA_API_KEY')}\n"
        f"TheSportsDB: {sportsdb_status}\n"
        f"ClubElo: {clubelo_status}\n"
        f"Understat: {understat_status}\n"
        f"FanGraphs/pybaseball: {fangraphs_status}\n"
        f"SerpApi: {serpapi_status}\n"
        "🌦 Weather API: ✅ Available\n\n"
        f"📊 Picks Tracked: {counts['total']}\n"
        f"💾 Results Database: {database_status}\n\n"
        "🤖 Current Version: BETGPTAI v1.2"
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
                save_official_picks,
                card,
                slate,
                official_sports_date().isoformat(),
            )
            print(f"Saved {saved_count} official picks to picks.json", flush=True)
        except Exception:
            logging.exception("Could not save /%s official picks", command)
        await _send_long_message(update, card)
    except Exception:
        logging.exception("Unexpected /%s error", command)
        await update.message.reply_text("Unable to build that MLB card right now.")


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
            save_official_picks, analysis, slate, selected_date
        )
        print(f"Saved {saved_count} official picks to picks.json", flush=True)
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
        "/status\n\n"
        "🧠 MODEL REPORT\n"
        "/model_report\n\n"
        "📊 UPDATE RESULTS\n"
        "/update_results\n\n"
        "✅ GRADE TODAY\n"
        "/grade_today\n\n"
        "🧪 DEBUG PICKS\n"
        "/debug_picks\n\n"
        "📝 BACKFILL PICKS\n"
        "/backfill_today\n\n"
        "📡 TEST CHANNELS\n"
        "/test_channels\n\n"
        "🖼 IMAGE CARD TESTS\n"
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
                sports_db_api_key=os.getenv("THE_SPORTS_DB_API_KEY", ""),
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
        asyncio.to_thread(
            api_sports_baseball_available,
            os.getenv("API_SPORTS_KEY", "") or os.getenv("API_FOOTBALL_KEY", ""),
        ),
        asyncio.to_thread(check_thesportsdb, os.getenv("THE_SPORTS_DB_API_KEY", "")),
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
            f"API-Sports Baseball status: {'✅ Available' if api_sports_baseball_ok else '❌ Unavailable'}\n"
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
        f"API-Sports Baseball status: {'✅ Available' if api_sports_baseball_ok else '❌ Unavailable'}\n"
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
        f"NRFI candidates: {report.get('nrfi_candidates', 0)}\n"
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


async def test_image_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: generate a fake Anime Vault MLB image and send it privately."""
    del context
    if not await _require_admin(update):
        return
    if not update.message:
        return
    try:
        image_path = await asyncio.to_thread(generate_test_mlb_card)
        with image_path.open("rb") as image_file:
            await update.message.reply_photo(
                photo=image_file,
                caption="BETGPTAI Anime Vault test MLB card",
            )
    except Exception as error:
        logging.exception("Test image card generation failed")
        await update.message.reply_text(
            f"❌ Image generation failed:\n{error}"
        )


async def post_test_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: send the fake Anime Vault MLB image to configured test destinations."""
    if not await _require_admin(update):
        return
    if not update.message:
        return
    destinations = (
        ("Free Channel", "FREE_CHANNEL_ID"),
        ("VIP Channel", "VIP_CHANNEL_ID"),
        ("Community Group", "COMMUNITY_GROUP_ID"),
    )
    try:
        image_path = await asyncio.to_thread(generate_test_mlb_card)
    except Exception as error:
        logging.exception("Could not generate test image for channel posting")
        await update.message.reply_text(f"❌ Image generation failed:\n{error}")
        return

    results: list[str] = []
    for label, environment_name in destinations:
        try:
            chat_id = _telegram_destination(os.getenv(environment_name, ""))
            with image_path.open("rb") as image_file:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_file,
                    caption=short_caption("test_mlb"),
                )
            results.append(f"{label}: ✅ Success")
        except Exception as error:
            logging.warning("Test image post failed for %s: %s", label, error)
            results.append(f"{label}: ❌ Failed\nError: {error}")

    await update.message.reply_text(
        "🖼 TEST IMAGE POST RESULTS\n\n" + "\n\n".join(results)
    )


async def public_button_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Make reply-keyboard labels behave like their matching public commands."""
    if not update.message or not update.message.text:
        return
    handlers = {
        "🎯 Today’s Picks": today,
        "⚾ MLB Card": mlb_auto,
        "⚽ Soccer Card": soccer,
        "📊 Results": results,
        "💎 Premium": vip,
        "ℹ️ Help": help_command,
    }
    handler = handlers.get(update.message.text)
    if handler:
        await handler(update, context)


async def _configure_command_menus(application: object) -> None:
    """Keep every Telegram command menu limited to public app navigation."""
    public_commands = [
        BotCommand("start", "Open BETGPTAI sports hub"),
        BotCommand("today", "Today's featured picks"),
        BotCommand("tomorrow", "Next-day cards"),
        BotCommand("mlb_auto", "Today's MLB card"),
        BotCommand("soccer", "Today's soccer card"),
        BotCommand("worldcup", "World Cup mode"),
        BotCommand("results", "Results tracker"),
        BotCommand("vip", "Premium access"),
        BotCommand("help", "Help menu"),
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
    token = os.getenv("BOT_TOKEN")

    # Stop early with a clear message if the private token is missing.
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Copy .env.example to .env and add your token."
        )

    # ApplicationBuilder creates the python-telegram-bot application.
    application = ApplicationBuilder().token(token).build()

    # Connect each Telegram command to its matching async function above.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("mlb_auto", mlb_auto))
    application.add_handler(CommandHandler("soccer", soccer))
    application.add_handler(CommandHandler("worldcup", worldcup))
    application.add_handler(CommandHandler("vip", vip))
    application.add_handler(CommandHandler("results", results))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("update_results", update_results))
    application.add_handler(CommandHandler("grade_today", grade_today))
    application.add_handler(CommandHandler("debug_picks", debug_picks))
    application.add_handler(CommandHandler("backfill_today", backfill_today))
    application.add_handler(CommandHandler("f5", specialized_mlb_card))
    application.add_handler(CommandHandler("nrfi", specialized_mlb_card))
    application.add_handler(CommandHandler("teamtotals", specialized_mlb_card))
    application.add_handler(CommandHandler("parlay", specialized_mlb_card))
    application.add_handler(CommandHandler("fullday", specialized_mlb_card))
    application.add_handler(CommandHandler("strikeouts", specialized_mlb_card))
    application.add_handler(CommandHandler("hits", specialized_mlb_card))
    application.add_handler(CommandHandler("home_runs", specialized_mlb_card))
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
    application.add_handler(CommandHandler("test_image_card", test_image_card))
    application.add_handler(CommandHandler("post_test_image", post_test_image))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, public_button_router)
    )

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
