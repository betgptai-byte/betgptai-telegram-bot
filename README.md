# BETGPTAI Telegram Bot

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
- `COMMUNITY_GROUP_ID`

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
