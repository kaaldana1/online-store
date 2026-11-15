# Setting up the store Discord bot

About 20–30 minutes the first time. You need a Discord account, and a computer with **Python 3.11 or newer** (check with `python3 --version`) that can stay on while the bot runs.

---

## 1. Create the server and channels (Discord app)

1. In Discord, click **+** at the bottom of the server list → **Create My Own** → **For me and my friends**. Name it something like "Store Team".
2. Create these **text channels** (click **+** next to "Text Channels"):
   - `#approvals`: the bot posts things that need your mom's OK, with Approve and Reject buttons
   - `#alerts`: sold-item delist warnings and errors
   - `#new-items`: where photos will go in step 2 (not used yet)
3. Optional for now: a **Forum** channel named `#offers`, for buyer conversations in step 4. If Discord doesn't offer the Forum type, skip it.
4. Invite your mom: right-click the server → **Invite People**. On her phone, set `#approvals` and `#alerts` to **All Messages** notifications (long-press the channel → Notifications).

## 2. Create the bot (Discord Developer Portal)

1. Go to https://discord.com/developers/applications and sign in → **New Application** → name it (e.g. "Store Bot") → **Create**.
2. Left menu → **Bot**:
   - Click **Reset Token** → **Yes** → **Copy**. Save the token somewhere safe for now. **This token is a password.** Never paste it in chat, screenshots, or GitHub. If it ever leaks, come back here and Reset it.
   - Turn **Public Bot** OFF, so only you can add it to servers.
   - Leave all three "Privileged Gateway Intents" OFF. The bot doesn't need them.
3. Left menu → **OAuth2** → **URL Generator**:
   - Scopes: tick **bot** and **applications.commands**.
   - Bot Permissions: tick **View Channels**, **Send Messages**, **Embed Links**, **Read Message History**, **Mention Everyone** (needed for @here alerts), **Create Public Threads**, **Send Messages in Threads**, **Manage Threads**.
   - Copy the **Generated URL** at the bottom, open it in your browser, pick your Store Team server → **Authorize**.
4. The bot now shows in your server's member list as offline. That's expected until you run it.

## 3. Copy the IDs the bot needs (Discord app)

1. **User Settings** (gear icon) → **Advanced** → turn on **Developer Mode**.
2. Right-click the **server icon** → **Copy Server ID**. That's your `GUILD_ID`.
3. Right-click `#approvals` → **Copy Channel ID**. That's `APPROVALS_CHANNEL_ID`.
4. Right-click `#alerts` → **Copy Channel ID**. That's `ALERTS_CHANNEL_ID`.

## 4A. Run it in Docker on WSL (recommended for you)

Skip 4B if you use this.

1. **Put the folder inside WSL, not on the Windows drive.** Open your Ubuntu (WSL) terminal and copy the `store-system` folder to `~/store-system`. Don't run it from `/mnt/c/...`: SQLite is slow there and can hit file-locking errors.
   ```bash
   cp -r /mnt/c/Users/<you>/Downloads/store-system ~/store-system
   cd ~/store-system
   ```
2. Create `bot/.env` with the four values from steps 2–3 (same format as shown in 4B below). It is passed into the container at runtime and is never built into the image.
3. Build and start:
   ```bash
   docker compose up -d --build
   docker compose logs -f        # expect "Logged in as Store Bot | approvals channel: OK | alerts channel: OK"
   ```
   Ctrl+C stops watching the logs, not the bot.
4. Everyday commands:
   | Do this | Command |
   |---|---|
   | Run the self-tests (step 5) | `docker compose exec storebot python bot/selftest.py` (add `--alert` or `--clean`) |
   | See logs | `docker compose logs -f` |
   | Restart after editing code or `fees.toml` | `docker compose up -d --build` |
   | Stop | `docker compose down` |
   The database is stored at `~/store-system/data/store.db` on the WSL side, so rebuilding the container never wipes it.
5. **Keeping it up 24/7 on Windows.** The container restarts by itself after crashes and Docker restarts (`restart: unless-stopped`), but Windows has to cooperate:
   - Docker Desktop → Settings → General → tick **Start Docker Desktop when you sign in**.
   - Windows Settings → System → Power → set **Sleep** to *Never* (when plugged in). The bot goes offline while the PC sleeps.
   - After a Windows reboot, Docker Desktop only starts once you sign in. Windows Update reboots will take it offline until someone logs in.
   - If you installed Docker Engine *inside* Ubuntu instead of using Docker Desktop, WSL can shut itself down when no terminal is open. Docker Desktop avoids this.

Then go to step 5 to test.

## 4B. Install and run without Docker (any computer)

Download the whole `store-system` folder from the project files. Then, in a terminal inside that folder:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create the file `bot/.env` (copy `bot/env.example.txt` and rename it, or create it fresh) containing:

```
DISCORD_TOKEN=paste-the-token-from-step-2
GUILD_ID=paste-server-id
APPROVALS_CHANNEL_ID=paste-approvals-channel-id
ALERTS_CHANNEL_ID=paste-alerts-channel-id
```

No quotes and no spaces around `=`. Then create the database and start the bot:

```bash
python db/init_db.py
python bot/bot.py
```

You should see a line like `Logged in as Store Bot`, and the bot turns green (online) in Discord. Leave this terminal open.

## 5. Test it

Open a **second** terminal in the same folder, activate the venv again, then:

| Test | Command / action | What you should see |
|---|---|---|
| Fee calculator | In Discord, type `/fees` and fill in price `40`, category `jewelry`, label `5.50`, cost `3` | A table: Facebook ~$37.00 (local pickup), eBay ~$29.23, Poshmark ~$29.00 net |
| Approvals | `python bot/selftest.py` | Within 30 s a gold "Needs approval" card appears in `#approvals`. Press **Approve** and it turns green. |
| Pending list | `/pending` in Discord | "Nothing waiting" (or any approvals still open) |
| Delist alerts | `python bot/selftest.py --alert` | Two red "SOLD: remove TEST-001…" alerts in `#alerts` (Facebook and Poshmark). Press **Done** on each. |
| Clean up | `python bot/selftest.py --clean` | Removes all test rows |

If all five work, setup is done.

## 6. Keep it running (without Docker)

If you used Docker (4A), you're already covered. Otherwise, the bot only works while `bot.py` is running. Options, from cheapest:

- **A computer that's always on** (old laptop, desktop, Raspberry Pi). On Linux or a Pi, make it start automatically:

  ```ini
  # /etc/systemd/system/storebot.service
  [Unit]
  Description=Store Discord bot
  After=network-online.target

  [Service]
  WorkingDirectory=/home/YOU/store-system
  ExecStart=/home/YOU/store-system/.venv/bin/python bot/bot.py
  Restart=always
  RestartSec=10
  User=YOU

  [Install]
  WantedBy=multi-user.target
  ```
  Then run `sudo systemctl enable --now storebot`, and check it with `journalctl -u storebot -f`.
- **A small cloud server** (~$4–6/month, e.g. any basic Linux VPS). Same systemd setup.
- **Just a terminal** works too, but the bot stops when the computer sleeps or the window closes.

The database lives in `data/store.db`. Back that one file up occasionally.

## Troubleshooting

| Problem | Fix |
|---|---|
| `KeyError: 'DISCORD_TOKEN'` | `bot/.env` is missing or misnamed. Windows often saves it as `.env.txt`; turn on "show file extensions" and rename it. |
| `ModuleNotFoundError: tomllib` | Python is older than 3.11. Install a newer Python. |
| `ModuleNotFoundError: discord` | The venv isn't active. Run the `source .venv/bin/activate` line again. |
| `Improper token has been passed` | The token was copied wrong or reset. Reset it in the portal and paste the new one. |
| `/fees` doesn't show up | Check `GUILD_ID` is the server ID, not a channel ID. Re-invite with the **applications.commands** scope. Restart Discord (Ctrl+R). |
| Bot is online but nothing appears in `#approvals` or `#alerts` | Channel IDs are wrong, or the bot can't see that channel. Check the channel's Permissions settings. |
| Alerts post but @here doesn't ping | Give the bot **Mention Everyone** (Server Settings → Roles → the bot's role). |
