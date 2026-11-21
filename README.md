# Store system

```
platforms/research.md   what each marketplace charges and allows (read this first)
platforms/fees.toml     the same fees as data, read by the pricing code. Edit when fees change.
pricing/fees.py         no-AI profit calculator: compare platforms, or solve for a target profit
db/schema.sql           one SQLite database for everything (items, bins, listings, buyers, approvals, costs)
db/init_db.py           creates data/store.db; --demo simulates a sale to show auto-delisting
bot/bot.py              Discord bot: approval buttons, delist alerts, /fees command
bot/listing_ui.py       step 2: /newitem → AI draft → comps → prices → approval → copy-paste packets
listing/ai.py           Claude + the resale-lister skill: identify from photos, draft per-platform listings, lint, retry once
skills/resale-lister/   the skill: SKILL.md, references/ (platform + category rules), scripts/lint_listing.py
listing/pricer.py       market price → list price + lowest offer per platform (rules, no AI)
listing/config.toml     step 2 knobs: AI model, price multipliers, min profit, label estimates
bot/web.py              step 3: storage map web server (http://localhost:8765) + its API
web/storage.html        step 3: the storage map page (bins, drag & drop, finder)
storage/search.py       step 3: finder (keyword index + optional AI pick)
bot/storage_cmds.py     step 3: /find, /bin, /move, /sold, /unsold
sales/email_watch.py    reads marketplace sale emails (IMAP, read-only) and matches them to items. No AI
sales/email_rules.toml  how sale emails are recognized per site, and the auto_record switch
sales/sale_record.py    record / undo a sale (used by /sold, /unsold and the email watcher)
bot/sales_watch.py      runs the email watcher inside the bot and posts its cards in #alerts
bot/ops.py              /toship, /shipped, /export (tax CSV), daily ship digest, nightly backup
```

Guides: `SETUP-discord-bot.md` (install), `TESTING.md` (step 1 tests), `STEP2-listing-assistant.md` (step 2), `STEP3-storage-map.md` (step 3), `SALES-EMAIL.md` (sale detection from email).

## Try it (no accounts needed)
```bash
python pricing/fees.py 40 --category jewelry --label 5.50 --cost 3
python pricing/fees.py --target-net 25 --category jewelry --label 5.50 --cost 3
python pricing/test_fees.py
python db/init_db.py --demo
python sales/test_sales.py
```

## Run the Discord bot
1. Create a Discord server with channels `#approvals`, `#alerts`, `#new-items`, and `#offers` (a Forum channel).
2. At https://discord.com/developers/applications, create an app, add a Bot, copy its token. Invite it with the `bot` and `applications.commands` scopes (Send Messages, Embed Links, Create Public Threads, Manage Threads).
3. In Discord, turn on Developer Mode (Settings › Advanced). Right-click the server and channels to copy their IDs.
4. `cp bot/.env.example bot/.env`, fill it in, then `pip install -r requirements.txt` and `python bot/bot.py`.

It runs fine on any always-on machine: an old laptop, a Raspberry Pi, or a ~$5/month VPS.

## How the pieces fit together
- **Approvals are rows, not chat.** Any agent that wants to post, change a price, or reply to a buyer inserts a row into `approvals`. The bot shows it with Approve and Reject buttons, and a worker applies it only after approval. That keeps "always ask permission" enforced in one place.
- **Selling one place delists everywhere.** Sales are detected from the marketplaces' sale emails (`SALES-EMAIL.md`) or recorded with `/sold`. Either way a row goes into `sales`, which fires a database trigger. The trigger creates a delist task for every other listing of that item, and each one shows up in `#alerts` with the listing link and the exact steps, re-pinging with @here every 2 hours until someone presses "Done". People do the delisting: nothing logs in to a marketplace (Poshmark bans automation). If the inbox can't be read, `#alerts` says so right away.
- **Buyer conversations nest item → platform → buyer.** `listings` is one row per item per platform, `buyer_threads` is one row per buyer on that listing, and `offers` and `messages` hang off each buyer. The `best_offers` view ranks every open offer on an item across all platforms by **what you actually keep after fees**, not by the offer amount. Planned Discord layout for step 4: one `#offers` forum post per active item, with one thread per buyer.
- **Gateway, starting small.** Every AI call gets logged to `agent_runs` with its cost. `spend_this_month` keeps you honest about the $50 budget.

## Backups
Every night at 3 AM the bot copies the database to `data/backups/store-YYYY-MM-DD.db` (the last 14 are kept). That only protects against a bad change, not a dead laptop, so also copy it off the machine:

1. Make a folder in OneDrive (or Google Drive for desktop), e.g. `C:\Users\you\OneDrive\store-backups`.
2. In `docker-compose.yml`, uncomment the `/app/offsite` line and put in that path (`/mnt/c/Users/you/OneDrive/store-backups`).
3. Add `BACKUP_OFFSITE=/app/offsite` to `bot/.env`, then `docker-compose up -d`.

The database and any new photos are then copied there nightly, and OneDrive syncs them to the cloud. If a backup fails, `#alerts` gets an @here. To restore: stop the bot, copy a backup over `data/store.db`, and start it again.
