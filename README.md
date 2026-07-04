# BETGPTAI Telegram Bot

## BETGPTAI v3.0 modular architecture

The project now includes a modular `bot/` package designed for scalable growth
without changing current user-facing behavior.

```text
bot/
├── app.py
├── config.py
├── constants.py
├── startup.py
├── handlers/
├── callbacks/
├── services/
├── api/
├── models/
├── menus/
├── utils/
├── diagnostics/
└── data/
```

### Compatibility phase

`main.py` remains the Railway-safe runtime entrypoint for now. The new v3
modules wrap the existing battle-tested implementation so commands, menus,
scheduler behavior, image generation, results, AI learning, Mission Control,
MLB War Room, and Player Props continue to behave the same.

Callback registration has moved to:

```text
bot/callbacks/router.py
```

`main.py` now calls that router instead of registering `CallbackQueryHandler`
directly. This keeps callback registration centralized while preserving all
existing inline menu behavior.

### Refactor rules going forward

- New code should read environment values from `bot/config.py`.
- New callback registration belongs only in `bot/callbacks/router.py`.
- New command modules belong in `bot/handlers/`.
- New business logic belongs in `bot/services/`.
- New inline keyboards belong in `bot/menus/`.
- Legacy top-level files are compatibility adapters until their logic is moved
  safely module-by-module.

## Safe local → GitHub → Railway workflow

BETGPTAI uses a simple production-safety workflow:

1. Local Mac is for editing and testing only.
2. GitHub is the source of truth.
3. Railway is production only.
4. Never run two Telegram polling instances with the same bot token.

### Environment detection

The bot treats the environment as:

```text
RAILWAY_ENVIRONMENT exists → railway
otherwise                  → local
```

When running locally, Telegram polling is blocked unless you explicitly set:

```env
LOCAL_BOT_ALLOWED=true
```

Keep this false unless Railway is paused/stopped:

```env
LOCAL_BOT_ALLOWED=false
APP_TIMEZONE=America/New_York
TZ=America/New_York
DATA_DIR=/data
```

If you try to start the bot locally without approval, it exits safely:

```text
Local bot blocked. Set LOCAL_BOT_ALLOWED=true only when Railway is paused.
```

### Daily development workflow

```text
Codex edits locally
→ Test non-bot modules locally
→ Commit and push to GitHub
→ Railway auto-deploys production
→ Verify with /version and /status
```

Local compile test:

```bash
scripts/test_local.sh
```

Deploy through GitHub:

```bash
scripts/deploy.sh "your commit message"
```

### `/version`

Use `/version` in Telegram to verify the running deployment:

```text
App Version
Git Commit
Environment: local or railway
Deploy Time
APP_TIMEZONE
DATA_DIR
```

## Pregame-only platform

BETGPTAI is designed as a pregame analysis platform, not a live score bot.

Disabled by design:

- Live score polling
- Inning updates
- Score notifications
- Live Telegram edit loops
- Live scheduler jobs
- Game-progress messages

Kept active:

- 45-minute pregame card scheduler
- Lineup verification
- Starting pitcher verification
- Player prop generation
- Anime image generation
- Official MLB card
- Official Soccer card
- Automatic grading
- Automatic results posting
- Admin approval workflow

### Scheduler behavior

```text
45 minutes before the first scheduled game
→ Generate today's cards and Anime image previews for owner approval

During games
→ No updates

After all saved official games finish
→ Grade today's picks
→ Generate daily results
→ Post results
```

The `/status` command reports `Live Updates: ➖ Disabled` so the owner can
confirm live updates are intentionally off.

## Admin-only hitting streak tracking

`hitting_streaks.py` enriches MLB player prop analysis with MLB Stats API game
logs. It is used only inside admin prop commands:

- `/props_admin`
- `/hits_admin`
- `/hits_by_team_admin`
- `/best_hit_image_admin`
- `/streak_report_admin`
- `/streak_debug_admin`

Hit props now consider active hit streaks, last-10 hit rate, multi-hit trend,
and cold-streak downgrades. Streaks are only a supporting factor; the engine
still requires player/team verification, lineup context, opponent matchup,
Statcast contact profile, park/weather, and bullpen context.

`/streak_report_admin` creates a daily research report of hitters batting 1-5
who have an active 2+ game hitting streak. Reports are saved to:

```text
DATA_DIR/hitting_streak_report_YYYY-MM-DD.json
```

This report is admin-only and does not automatically recommend a bet.

## BETGPTAI Anime Edition v7.0

BETGPTAI Anime Edition v7.0 adds a visual-first MLB carousel workflow for the
owner-only `/mlb_images` command.

### `/mlb_images`

When you run `/mlb_images`, the bot:

1. Builds today’s official MLB card using the existing MLB analysis pipeline.
2. Converts the card into seven 1080x1920 vertical carousel slide prompts.
3. Sends the seven ready-to-copy prompts back to Telegram.
4. Saves prompts into `generated_cards/YYYY-MM-DD/`.
5. If `IMAGE_GENERATION_ENABLED=true`, generates seven owner-only preview
   images with the OpenAI Images API.
6. Saves generated images into `generated_cards/YYYY-MM-DD/`.

```text
generated_cards/YYYY-MM-DD/slide_1_prompt.txt
generated_cards/YYYY-MM-DD/slide_2_prompt.txt
generated_cards/YYYY-MM-DD/slide_3_prompt.txt
generated_cards/YYYY-MM-DD/slide_4_prompt.txt
generated_cards/YYYY-MM-DD/slide_5_prompt.txt
generated_cards/YYYY-MM-DD/slide_6_prompt.txt
generated_cards/YYYY-MM-DD/slide_7_prompt.txt
generated_cards/YYYY-MM-DD/slide_1.png
generated_cards/YYYY-MM-DD/slide_2.png
...
```

The bot does not create placeholder graphics and does not use Pillow for final
cards.

### Enable image generation

Prompt-only mode:

```env
IMAGE_GENERATION_ENABLED=false
```

Owner-only preview image mode:

```env
IMAGE_GENERATION_ENABLED=true
OPENAI_API_KEY=your_openai_key
OPENAI_IMAGE_MODEL=gpt-image-1
OPENAI_IMAGE_SIZE=1024x1536
```

The prompts request a 1080x1920 vertical card. `gpt-image-1` supports portrait
sizes such as `1024x1536`, which is used as the default closest supported
vertical format. If your selected model supports exact `1080x1920`, update
`OPENAI_IMAGE_SIZE`.

### `/post_mlb_images`

`/post_mlb_images` is owner-only. It posts the approved generated images from
`generated_cards/YYYY-MM-DD/` to:

- `FREE_CHANNEL_ID`
- `VIP_CHANNEL_ID`

Automatic image posting is not enabled. Run `/post_mlb_images` only after
reviewing the `/mlb_images` owner preview.

### Workflow

```text
Generate Picks
→ Generate 7 Anime Vault prompts
→ Send prompts via /mlb_images
→ Save prompts in generated_cards/YYYY-MM-DD/
→ If enabled, generate owner-only image previews
→ Review images
→ Manually post approved images with /post_mlb_images
```

### Customize mascot prompts

Edit `mascot_style.py`.

The `MLB_MASCOT_STYLE` dictionary maps MLB teams to their Anime Edition mascot
prompt style. Example:

```python
"Milwaukee Brewers": "anime Bernie Brewer, high-energy anime sports trading card..."
```

Change the value for any team to adjust its mascot look, energy, colors, or
pose direction.

Every mascot prompt should preserve the Anime Vault quality rules:

- Aggressive action pose
- Glowing eyes
- Dynamic manga speed lines
- Dramatic perspective
- Premium sports-card illustration
- Electric lightning effects
- Stadium atmosphere
- Cel-shaded anime rendering
- Hyper-detailed uniform
- Team-color energy effects
- High-detail mascot face
- Championship-level intensity
- Topps Chrome sports card aesthetic
- Blue Lock sports anime energy
- Premium collectible card quality
- Mascot/player occupies 35–45% of the composition

### Change slide layout

Edit `card_image_generator.py`.

The main functions are:

```python
create_slide_prompt(slide_number: int, card_data: dict) -> str
generate_mlb_card_slides(card_data: dict) -> list[str]
```

Update `create_slide_prompt` to change:

- Slide titles
- Visual themes
- Panel structure
- Text hierarchy
- Mascot placement
- Carousel sequence

Current v7.0 slide structure:

1. Play of the Day
2. Top 5 Moneylines
3. Top 5 F5 Plays
4. Top 5 Run Lines
5. Game Totals + Team Totals
6. The Vault
7. Follow @BETGPTAI + disclaimer

The image prompts intentionally avoid confidence meters, internal model rules,
API names, and long betting explanations. They use short clean text, team
colors, anime mascot artwork, electric effects, and premium sports-card styling.

Do not generate fake placeholder graphics, emoji mascots, flat infographic
templates, or generic anime posters.
