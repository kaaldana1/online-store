# Step 2: Listing assistant

Photo in, ready-to-paste listings out. The bot never posts, prices, or messages on a platform by itself; your mom approves each item and does the posting.

## How your mom uses it (about 2 minutes per item)

1. **`/newitem`** in any channel (`#new-items` is a good home). Attach the main photo, plus up to 2 more (hallmark, label, flaw). Optionally add notes ("925 stamp, small dent"), what she paid, and the bin number.
2. Within ~15 seconds the bot replies with an **intake card**: SKU (e.g. `S0007`), what it thinks the item is, condition, estimated weight, and **⚠️ checks** like "photograph the clasp hallmark".
3. She taps **🔎 eBay sold prices** (or Poshmark sold). That opens real sold listings for the AI's search phrase, and she eyeballs the typical price.
4. She taps **💲 Enter prices** and types the typical sold price (plus an optional range). Weight and cost come prefilled.
5. The bot works out each platform's list price, lowest offer to accept, and profit after fees. It skips any platform under the minimum profit, then posts the item to **#approvals**.
6. She taps **Approve**. A thread opens with one **copy-paste packet per platform** (title, price, tags, description in grey boxes; long-press to copy).
7. After posting on each site, she taps **📤 Mark posted**. The bot replies "Today: 3/5 items listed".

Other commands: **`/today`** shows progress toward 5 a day, **`/spend`** shows AI cost this month, and `/fees` and `/pending` work as before.

## Setup (you, once)

1. **Get an Anthropic API key:** https://console.anthropic.com → Settings → API keys → Create key. Add a few dollars of credit under Billing. A spend limit (e.g. $10/month) is a good idea.
2. Add it to `~/store-system/bot/.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Copy the new and changed files from the project files into `~/store-system/`, keeping the folder structure:
   - new: `listing/` (whole folder), `bot/listing_ui.py`, `STEP2-listing-assistant.md`
   - changed: `bot/bot.py`, `requirements.txt`, `pricing/fees.py`, `platforms/fees.toml`
4. Rebuild:
   ```
   docker compose up -d --build        # or: docker-compose down && docker-compose up -d --build
   docker compose logs -f
   ```
5. In Discord, press Ctrl+R (or restart the app on phones). `/newitem`, `/today` and `/spend` should appear.

Photos are saved in `~/store-system/data/photos/` (named by SKU), and everything else in the same database as before.

## Test it

| # | Do this | Pass if |
|---|---|---|
| 1 | `/newitem` with a photo of anything (a mug is fine), notes "test", cost 1 | Intake card with an AI title, a "What it's based on" section and the three buttons. `/spend` shows 1 run, about $0.05–0.15 |
| 1b | `/newitem` with a clear photo of a hallmark or a Coach creed | Card names the maker/style, and may list a 🔗 source. If it says "Web search is off", enable web search for your API organization in the Claude Console |
| 2 | Tap **eBay sold prices** | eBay opens on *sold* listings for the AI's search phrase |
| 3 | Tap **Enter prices**, typical 40, range 30-55 | Reply lists each platform's price/floor/profit. A card appears in #approvals |
| 4 | Enter prices again with typical **6** on a new test item | Some or all platforms "skipped" for low profit |
| 5 | **Approve** the card from #3 | A thread with one packet per platform, each with a **Mark posted** button |
| 6 | **Mark posted** on one packet | "Today: 1/5". `/today` matches |
| 7 | **Reject** a different item's card | Card turns red. Tapping Enter prices on that item again re-prices it |
| 8 | Remove the API key from `.env`, `docker compose up -d`, try `/newitem` | Item still saved with "saved without an AI draft" and the reason, and pricing still works. Put the key back |

What I could and couldn't test here: the whole flow ran end to end with a simulated Discord and a simulated AI answer, the real API call reached Anthropic (with a fake key, so it got the expected "invalid key" error, handled cleanly), and the Docker image builds with the new packages. I haven't seen a real AI draft yet, because that needs your key. Test #1 is the one to watch.

## Tuning (`listing/config.toml`)

- `multiplier`: list price vs. market per platform (Poshmark ×1.15 for offer room, Facebook ×0.90 for quick local cash).
- `min_profit` ($5) skips a platform below it. `keep_fraction` (0.75) sets the lowest acceptable offer.
- `label_estimate`: rough shipping by weight, used for eBay's fee on shipping.
- `model` / `effort`: the AI model. With the resale-lister skill, Opus 5.5 at low effort costs roughly 8–15¢ per item (the skill's instructions are cached, so items done within 5 minutes of each other are cheaper), about $12–22/month at 5 a day. `/spend` shows the real number. `claude-haiku-5-5` is much cheaper if its drafts are good enough.
- `web_search` / `max_web_searches`: lets Claude look up maker's marks, flatware patterns, Coach style numbers and RN numbers (never prices), up to 3 searches per item at 1¢ each.
- `[facebook] pickup_area`: shown on every Facebook packet, e.g. "Mesa, near Main & Dobson". Never put your address here.

Rebuild after editing (`docker compose up -d --build`).

## The resale-lister skill

The drafting rules live in `skills/resale-lister/` as plain text, so you can read and edit them:
`SKILL.md` (the workflow), `references/` (one file per platform, item checks, and one per category: jewelry,
clothing, sterling tableware, handbags, general), and `scripts/lint_listing.py` (a no-AI checker).

What happens on `/newitem`:
1. Claude reads every photo, records the marks it sees as evidence, runs the item checks, and may search the
   web (identification only), then submits the draft.
2. `lint_listing.py` checks it: title lengths, "sterling" on plate, "gold" on gold tone, other platforms or
   Venmo in the description, "Coach inspired", Native American claims, restricted materials not flagged.
   It also adds `SKU: S0042` as the description's last line.
3. If it finds errors, they go back to Claude once to fix. Anything still wrong shows on the intake card,
   the approval card and every packet as 🛑. Minor issues show as ✏️ on the intake card.
4. Packets now include eBay item specifics, Poshmark brand/category/size/colors/3 style tags, and Facebook
   tags plus your pickup area.

The bot never posts or changes prices on its own; 🛑 items still go through your approval, so read them first.

Edit any `.md` file in the skill, then `docker-compose up -d --build`. To use the same skill in the Claude app,
zip the folder: `cd skills && zip -r resale-lister.skill resale-lister`.

Run the offline tests: `docker-compose exec storebot python listing/test_listing.py` (eight `ok` lines).

## Coming later
- When eBay approves your developer account, comps will be pulled automatically from active eBay listings, and the eBay packet becomes a real "post to eBay" step.
- Step 3 adds the storage map, the bin finder, and multi-platform delist safety before items go live in more than one place at once.
