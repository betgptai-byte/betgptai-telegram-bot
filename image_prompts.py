"""Prompt-only BETGPTAI Anime Vault image direction.

The previous Pillow-only visual card path created placeholder graphics that did
not match the BETGPTAI Anime Vault brand. For now, the bot returns production
image prompts only. Real generated artwork should be created first; Pillow can
be reintroduced later only for placing final text over approved artwork.
"""

from __future__ import annotations


REQUIRED_NEGATIVE_STYLE = (
    "no emojis, no smiley faces, no placeholder icons, no flat infographic style"
)


def mlb_anime_vault_prompts() -> list[str]:
    """Return seven ready-to-copy prompts for image generation tools."""
    return [
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault official MLB card "
            "poster titled “THE VAULT”. High-energy anime baseball sports poster, "
            "dark cinematic stadium background, electric blue and yellow lightning, "
            "red and green neon accents, manga speed lines, dramatic smoke, glowing "
            "panel borders, premium Topps/ESPN/anime trading card energy. Main hero: "
            "an original fierce anime baseball slugger mascot in navy and gold, "
            "swinging a bat in an action pose, intense eyes, detailed uniform, sparks "
            "flying from the bat. Add right-side stacked panels for Best Moneyline, "
            "Best Underdog, Best Runline, Best Total, Best Team Total, plus lower "
            "panels for Safe Parlay, Value Parlay, and Core Five Plays. Branding text: "
            "BETGPTAI, The Odds Reaper, Stack Edges, Not Emotions. Leave clean high-contrast "
            "space inside each panel for later text overlay. Use bold brush typography, "
            "glowing borders, manga panel composition, premium betting poster style. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault “BEST BET” MLB hero card. "
            "Make it look like a premium anime sports trading card poster with a dark "
            "electric stadium, gold lightning, explosive manga action lines, glowing "
            "yellow title treatment, and cinematic rim lighting. Feature an original "
            "anime baseball power hitter mascot in a dramatic full-body action pose, "
            "bat over shoulder, fierce expression, detailed jersey, sweat, sparks, "
            "and stadium lights behind him. Center panel must feel massive and premium, "
            "with space for pick text, confidence grade, matchup, and one short reason. "
            "Branding: BETGPTAI / The Odds Reaper / The Vault. Style: neon yellow, "
            "electric blue, red heat accents, sharp manga ink shadows, bold brush fonts, "
            "high contrast, social-media ready. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 BETGPTAI Anime Vault MLB moneyline/runline card. "
            "Design a split-panel anime baseball battle poster: two original team-inspired "
            "anime mascots facing off in a dramatic stadium, one roaring in the foreground, "
            "the other charging from the opposite side. Use electric blue lightning on one "
            "side, red/orange fire on the other, gold glowing borders, manga action panels, "
            "and a premium betting-card layout. Include sections labeled Best Moneyline, "
            "Best Runline, Best Underdog, and Confidence Grade with blank readable space "
            "for later overlay. Branding: BETGPTAI, The Odds Reaper, Stack Edges Not Emotions. "
            "Use bold brush typography, dynamic perspective, anime trading card quality, "
            "dramatic shadows, detailed uniforms, and stadium floodlights. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault MLB totals card focused on "
            "run environment. Dark cinematic ballpark at night, wind trails, glowing rain "
            "particles, lightning over the scoreboard, fiery baseball streaking through "
            "the air. Feature two original anime baseball hitters in action poses, one "
            "launching a ball, one watching the blast, with manga impact bursts and neon "
            "red/yellow/blue panels. Include premium sections for Best Total, Team Total "
            "Angle, Safer Line, Park/Weather Edge, and Confidence Grade, leaving clean "
            "blank space for text overlay. Branding: BETGPTAI / The Odds Reaper / The Vault. "
            "Make it intense, collectible, glossy, and social-media ready. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault Safe Parlay MLB card. "
            "Premium anime poster layout with two original baseball anime mascots side by "
            "side as parlay legs, separated by a glowing plus sign made of lightning, not "
            "a simple icon. Dark electric stadium background, green and gold neon glow, "
            "manga panel borders, dramatic action poses, detailed jerseys, intense eyes, "
            "sparks, smoke, and stadium floodlights. Add sections labeled Safe Parlay of "
            "the Day, Leg 1, Leg 2, BETGPTAI Note, Singles First. Leave clean panel space "
            "for later text overlay. Branding: BETGPTAI, The Odds Reaper, The Vault. "
            "Style must feel like a premium anime betting poster/trading card, not a template. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault Value Parlay MLB card. "
            "Use a darker, more aggressive purple/blue/red neon theme with three original "
            "anime baseball characters in separate manga action panels, each representing "
            "a parlay leg. Add electric cracks, glowing borders, brush typography, dramatic "
            "stadium lights, smoke, and motion blur. Include premium text areas for Value "
            "Parlay, Leg 1, Leg 2, Leg 3, Confidence Grade, and Parlay Risk Note. Branding: "
            "BETGPTAI / The Odds Reaper / The Vault. Make the image feel like a high-end "
            "anime sports poster and collectible trading card with cinematic lighting and "
            "deep contrast. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
        (
            "Create a 1080x1920 vertical BETGPTAI Anime Vault Core Five MLB plays poster. "
            "Design a premium anime sports magazine cover mixed with trading card layout: "
            "bottom-right original anime “Odds Reaper” character pointing toward the viewer, "
            "electric blue aura, dark hoodie with BETGPTAI branding, intense expression, "
            "lightning all around. Upper area has a huge brush title “CORE FIVE PLAYS” with "
            "five stacked glowing manga panels for picks, each with a small original baseball "
            "mascot artwork vignette, color-coded neon borders, and readable blank areas for "
            "later text overlay. Add brand philosophy strip: Research, Discipline, Bankroll, "
            "Consistency. Dark cinematic stadium, yellow lightning, red/green/blue accents, "
            "bold brush typography, high-energy anime poster composition. "
            f"{REQUIRED_NEGATIVE_STYLE}."
        ),
    ]


def format_mlb_image_prompts() -> str:
    """Format prompts for Telegram without generating or posting images."""
    prompts = mlb_anime_vault_prompts()
    blocks = [
        "🖼 BETGPTAI ANIME VAULT MLB IMAGE PROMPTS\n\n"
        "Image generation is prompt-only right now. Pillow final-card generation "
        "is disabled until real anime artwork matches the uploaded reference.\n\n"
        "Copy any prompt below into DALL-E or your preferred image generator."
    ]
    for index, prompt in enumerate(prompts, start=1):
        blocks.append(f"━━━━━━━━━━━━\n\nPrompt {index}\n\n{prompt}")
    return "\n\n".join(blocks)
