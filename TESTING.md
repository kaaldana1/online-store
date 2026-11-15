# Testing the bot (docker-compose on WSL)

Run everything from `~/store-system` while the container is up (`docker-compose ps` shows `storebot  Up`).
Shortcut used below: `x` = `docker-compose exec storebot`. You can make it real with `alias x='docker-compose exec storebot'`.

**First, get the new helper script into the container.** Copy `bot/dbcheck.py` from the project files into `~/store-system/bot/`, then run `docker-compose up -d --build`. Code is baked into the image, so you rebuild after copying in any changed file.

| # | What you're testing | Do this | Pass if |
|---|---|---|---|
| 0 | Bot is connected | `docker-compose logs --tail 20` | You see `Logged in as … approvals channel: OK \| alerts channel: OK` |
| 1 | Fee math | `x python pricing/test_fees.py` | Five lines starting with `ok` |
| 2 | `/fees` command | In Discord: `/fees` with price 40, category jewelry, label 5.50, cost 3 | Facebook ≈ $37.00 (local pickup), eBay ≈ $29.23, Poshmark ≈ $29.00 |
| 3 | Approve | `x python bot/selftest.py`, wait ≤ 30 s, press **Approve** in #approvals | Card turns green with "approved by <you>". `x python bot/dbcheck.py` shows `status=approved` |
| 4 | Reject | Run `x python bot/selftest.py` again, press **Reject** | Card turns red. dbcheck shows `status=rejected` |
| 5 | Buttons survive a restart | `x python bot/selftest.py`, wait for the card, **don't press it**, then `docker-compose restart`. When logs say Logged in, press Approve on that same card | It still works (turns green) |
| 6 | `/pending` | Run `x python bot/selftest.py`, then type `/pending` in Discord before pressing anything | It re-posts the waiting card |
| 7 | Delist alerts | You already did `--alert` ✅. Now press **Done, I delisted it** on both alerts | Each alert changes to "✅ Delisted". dbcheck shows both delist tasks `status=done` |
| 8 | @here re-ping | Run `x python bot/selftest.py --alert` and **don't** press Done | The first alert has no @here. After 2 hours the same alert is posted again with @here (it repeats every 2 h until Done). To check without waiting, run `docker-compose restart`: the alert re-posts right away, without @here |
| 9 | Data survives a rebuild | `x python bot/dbcheck.py` (note the counts), `docker-compose down`, `docker-compose up -d --build`, run dbcheck again | Same rows as before. The database lives in `~/store-system/data/store.db`, outside the container |
| 10 | Your mom's phone | Have her press a button on a test approval from her phone | It works, and "approved by" shows her name |

**Clean up when done:** `x python bot/selftest.py --clean` removes all test rows. Old test messages stay in Discord, so delete them by hand or right-click the channel → Clone Channel for a clean one. If you clone, update the channel ID in `bot/.env` and run `docker-compose up -d`.

## If something fails
- **Card never appears:** `docker-compose logs --tail 50`. "NOT FOUND" next to a channel means the ID in `bot/.env` is wrong. After fixing it, `docker-compose up -d` reloads the env file. A plain `restart` does **not** reload it.
- **Pressing a button says "This interaction failed":** the bot was offline at that moment. Check `docker-compose ps`, then press again.
- **`exec` says the service isn't running:** `docker-compose up -d`. If Docker itself is down after a WSL restart: `sudo service docker start`.
