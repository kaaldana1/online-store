# Step 3: Storage map, finder, and sold safety

## What's new
- **Storage map web page** at **http://localhost:8080**, served by the bot itself (no extra container).
  - **Edit layout** lets you add bins, drag them to match your real shelves, resize them with the blue corner, and rename or delete them. Bins can't overlap.
  - Items from `/newitem` land in **"Not in a bin yet"**. Drag one onto a bin, or use its **Move to bin…** menu, which also works on touch screens.
  - Drag **photos from your computer** straight onto a bin to log a quick item there (it asks for a name).
  - Click a bin to see what's in it.
- **Finder.** Type "golden chalice with vine engravings" and press **Find**. Matching bins glow yellow, and clicking a result jumps to its bin. Tick **Smart** to let the AI pick the real match, which understands goblet ≈ chalice and gold-tone ≈ golden (fractions of a cent per search).
- **Discord commands**
  - `/find golden chalice with vine engravings` returns the top matches with photos and bin numbers (Smart is on by default). The title links to the map with that bin highlighted, and that link only works on the PC running the bot.
  - `/bin 4` shows what's in bin 4.
  - `/move S0007 4` puts an item in bin 4 (`0` takes it out).
  - `/sold S0007 poshmark 48` records a sale and tells you which bin it's in. **Every other platform it's still live on gets a red alert in #alerts** that re-pings every 2 hours until someone taps Done. Listings that were approved but never posted are simply closed.
- **Safety net:** any automatic delist that hasn't happened within 10 minutes also raises an alert. Right now every delist is a human task. eBay becomes automatic once its API is approved.

## Setup
1. Copy into `~/store-system/` (keep folders):
   - new: `storage/` (folder), `web/` (folder), `bot/web.py`, `bot/storage_cmds.py`, `STEP3-storage-map.md`
   - changed: `bot/bot.py`, `listing/ai.py`, `db/schema.sql`, `docker-compose.yml`, `Dockerfile`
2. Rebuild:
   ```
   docker compose up -d --build     # or: docker-compose down && docker-compose up -d --build
   docker compose logs -f           # look for "Storage map at http://localhost:8080"
   ```
3. Open **http://localhost:8080** in a browser on the same Windows PC. WSL forwards localhost automatically.
4. Click **Edit layout → + Add bin** a few times and arrange the bins like your shelves. Bin numbers count up from 1, so label the real bins to match. Then click **Done editing**.

**Phone access:** for safety the page only listens on this computer. To use it from your mom's phone later, the easiest safe route is Tailscale (free): install it on the PC and her phone, change the compose port line to `"8080:8080"`, and set `STORAGE_URL` in `bot/.env` to the PC's Tailscale address so `/find` links open on her phone too. Until then, `/find`, `/bin`, `/move` and `/newitem … bin:` cover her day-to-day from Discord.

## Test it
| # | Do this | Pass if |
|---|---|---|
| 1 | Open http://localhost:8080 | The page loads; your `/newitem` items are under "Not in a bin yet" |
| 2 | Edit layout → add 3 bins, move and resize them, Done | Positions stick after refreshing the page |
| 3 | Drag an item card onto a bin | Toast "S0001 → bin 2", and the bin's count goes up |
| 4 | Drag a photo file from your desktop onto a bin, give it a name | It appears in that bin |
| 5 | Search a few words describing one item | Its bin glows yellow; clicking the result opens that bin |
| 6 | Tick Smart and search with a synonym ("goblet" for a chalice) | It still finds it. `/spend` shows a "finder" run |
| 7 | `/find …`, `/bin N`, `/move SKU N` in Discord | Answers match the map (refresh the page) |
| 8 | Mark a test item posted on 2 platforms (step 2 flow), then `/sold SKU poshmark 30` | Reply names the bin, and a red alert for the other platform appears in #alerts |

## What I tested
The map ran in a real browser (Chromium via Playwright) against a test database. Dragging a card onto a bin, the Move-to-bin menu, adding, moving and resizing bins (including blocking overlaps), search with highlight, `?highlight=` links, and phone width (no sideways scroll) all worked, with no page errors. `/find`, `/bin`, `/move` and `/sold`, plus the alerts they trigger, were exercised with a simulated Discord. The Smart AI pick fell back to keyword search because I don't have your key, so test #6 is the one to watch.
