# Store system

```
platforms/research.md   what each marketplace charges and allows (read this first)
platforms/fees.toml     the same fees as data, read by the pricing code. Edit when fees change.
pricing/fees.py         no-AI profit calculator: compare platforms, or solve for a target profit
db/schema.sql           one SQLite database for everything (items, bins, listings, buyers, approvals, costs)
db/init_db.py           creates data/store.db; --demo simulates a sale to show auto-delisting
bot/bot.py              Discord bot: approval buttons, delist alerts, /fees command
bot/listing_ui.py       step 2: /newitem → AI draft → comps → prices → approval → copy-paste packets
listing/ai.py           one Claude call: identify item from photos, draft titles/description/tags
listing/pricer.py       market price → list price + lowest offer per platform (rules, no AI)
listing/config.toml     step 2 knobs: AI model, price multipliers, min profit, label estimates
bot/web.py              step 3: storage map web server (http://localhost:8080) + its API
web/storage.html        step 3: the storage map page (bins, drag & drop, finder)
storage/search.py       step 3: finder (keyword index + optional AI pick)
bot/storage_cmds.py     step 3: /find, /bin, /move, /sold
```

Guides: `SETUP-discord-bot.md` (install), `TESTING.md` (step 1 tests), `STEP2-listing-assistant.md` (step 2), `STEP3-storage-map.md` (step 3).

## Try it (no accounts needed)
```bash
python pricing/fees.py 40 --category jewelry --label 5.50 --cost 3
python pricing/fees.py --target-net 25 --category jewelry --label 5.50 --cost 3
python pricing/test_fees.py
python db/init_db.py --demo
```

## Run the Discord bot
1. Create a Discord server with channels `#approvals`, `#alerts`, `#new-items`, and `#offers` (a Forum channel).
2. At https://discord.com/developers/applications, create an app, add a Bot, copy its token. Invite it with the `bot` and `applications.commands` scopes (Send Messages, Embed Links, Create Public Threads, Manage Threads).
3. In Discord, turn on Developer Mode (Settings › Advanced). Right-click the server and channels to copy their IDs.
4. `cp bot/.env.example bot/.env`, fill it in, then `pip install -r requirements.txt` and `python bot/bot.py`.

It runs fine on any always-on machine: an old laptop, a Raspberry Pi, or a ~$5/month VPS.

## How the pieces fit together
- **Approvals are rows, not chat.** Any agent that wants to post, change a price, or reply to a buyer inserts a row into `approvals`. The bot shows it with Approve and Reject buttons, and a worker applies it only after approval. That keeps "always ask permission" enforced in one place.
- **Selling one place delists everywhere.** Inserting a row into `sales` fires a database trigger. The trigger creates a delist task for every other live listing of that item. API platforms (eBay) get delisted by a worker. Manual ones (Facebook, Poshmark), and any failed API call, show up in `#alerts` with an @here every 2 hours until someone presses "Done".
- **Buyer conversations nest item → platform → buyer.** `listings` is one row per item per platform, `buyer_threads` is one row per buyer on that listing, and `offers` and `messages` hang off each buyer. The `best_offers` view ranks every open offer on an item across all platforms by **what you actually keep after fees**, not by the offer amount. Planned Discord layout for step 4: one `#offers` forum post per active item, with one thread per buyer.
- **Gateway, starting small.** Every AI call gets logged to `agent_runs` with its cost. `spend_this_month` keeps you honest about the $50 budget.
