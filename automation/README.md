# Browser automation: Poshmark auto-delist

When an item sells somewhere else, this removes its Poshmark listing for you. It runs on your
computer in a visible browser window, using a Poshmark login you save once. The design and
reasoning are in the [recommendation doc](https://claude.ai/artifact/1tMahX8ePp9NNmYzCVW8UE).

All commands are in `automation/poshmark.py`. Run them from `~/store-system` with the
virtual environment on (`source .venv-automation/bin/activate`).
**Always put links in quotes.** Links often contain `&`, and without quotes the terminal breaks
the link apart.

| Command | What it does | Changes anything? |
|---|---|---|
| `login` | Opens a window so you can log in once | No |
| `check "<link>"` | Finds the Delete Listing button and draws a red box around it | **No**, it never clicks |
| `delist "<link>"` | Deletes one listing, then confirms it's gone (asks you to type `DELETE`) | **Yes** |
| `worker` | Watches the database and delists Poshmark listings of items sold elsewhere | **Yes**, only after a sale |
| `add-test-listing "<link>"` | Adds a TEST item for a listing, to try the whole flow | Only your database |

(`poshmark_check.py` still works too. It's the old name for the same thing.)

## How a delist works

1. Open the listing, then its edit page, and wait for the form to load.
2. Click **Delete Listing**, then the **Yes/confirm** button in the popup.
3. Reload the edit page. **It only counts as done if Poshmark says the listing no longer
   exists.** If the listing is still there, or the page never loads, that is a failure.
4. Post the result to `#alerts` with a screenshot: ✅ if it worked, ⚠️ if not.

**Safety guard.** A guard inside the browser blocks every request that could change your account.
It's lifted only for the few seconds between clicking Delete Listing and the confirm button,
then turned back on before step 3. The script stops and alerts you on a CAPTCHA or a login page.
It never tries to get past them.

**What happens when something fails.** The worker marks the task failed. The bot then shows it
in `#alerts` with the usual **Done, I delisted it** button and re-pings every 2 hours. A plain
error like a timeout gets one retry a minute later. If the worker isn't running at all, the bot
still alerts after 10 minutes, so a sale can't slip through.

## One-time setup for auto-delist

1. Let your user write to the database. The bot's Docker container creates it as root.
   ```bash
   cd ~/store-system && sudo chown -R $USER:$USER data
   ```
2. Turn it on. Add this line to `bot/.env`:
   ```
   AUTO_DELIST=poshmark
   ```
   Then restart the bot so it sees the change: `docker compose up -d --build`
   (or `docker-compose up -d --build`).
3. Save the link when you post. **Mark posted** now asks for the listing's link. Paste the
   Poshmark link there, because without it the worker can't find the listing. You can leave it
   blank for other sites.

## Test it on the pirate hat (do this first)

This runs the full flow, including the bot, on a listing you don't mind losing.

```bash
cd ~/store-system && git pull
source .venv-automation/bin/activate

# 1. make sure the login still works (safe, no clicking)
python automation/poshmark.py check "https://poshmark.com/listing/Pirate-Hat-6afa61daa6935870035b2652"

# 2. add the hat to the database as a TEST item
python automation/poshmark.py add-test-listing "https://poshmark.com/listing/Pirate-Hat-6afa61daa6935870035b2652"

# 3. make sure a worker is running: the Docker one (see below), or this in a terminal
python automation/poshmark.py worker
```

4. In Discord, fake a sale of the hat on Facebook with `/sold sku:TEST-PM1 platform:facebook price:1`.
   The bot should reply that it's removing the hat from Poshmark automatically.
5. Within about a minute, the browser window opens, deletes the hat, checks it's gone, and posts
   ✅ with a screenshot in `#alerts`. Open the hat's link on your phone to confirm it's gone.

To test only the delete part without the bot, use
`python automation/poshmark.py delist "<link>"` and type `DELETE` when it asks.

## Run the worker in Docker (recommended)

The worker has its own container, `poshmark-worker`, next to the bot. It starts and restarts
with the bot, so you don't need a terminal open. Inside, it runs a normal Chromium window on a
virtual screen (Xvfb), not headless mode, so Poshmark sees the same kind of browser as when you
run it yourself. It uses the login you saved with `login`, which lives in `browser-profiles/`
on this computer.

One-time setup, after `git pull`:

```bash
cd ~/store-system
id -u                      # if this is NOT 1000, run:  echo "WORKER_UID=$(id -u)" >> .env && echo "WORKER_GID=$(id -g)" >> .env
docker compose up -d --build        # or: docker-compose up -d --build
docker compose logs -f poshmark-worker
```

The logs should say `Watching for Poshmark delists every 60s.` Press Ctrl+C to stop following
the logs. The worker keeps running.

- **Stop any terminal or tmux worker** you started before. Only one worker runs at a time. A
  second one waits, or with `--once`, refuses to start.
- **Log in again** (when an alert says the login expired):
  `docker compose stop poshmark-worker`, then `python automation/poshmark.py login` in the venv,
  then `docker compose start poshmark-worker`. Only one browser can use the saved login at once.
- **Turn it off:** remove `AUTO_DELIST=poshmark` from `bot/.env`, then run
  `docker compose up -d`. The worker then waits quietly instead of restarting over and over.

The venv is still needed for `login`, `check`, `delist` and `add-test-listing`, because those
need a window you can see.

## Without Docker

Run `python automation/poshmark.py worker` in a terminal and leave it open. Stop it with Ctrl+C.
While no worker is running, Poshmark delists fall back to manual alerts after 10 minutes.

## First-time install

On Linux (ChromeOS Linux, or WSL on Windows 11):

```bash
cd ~/store-system
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv .venv-automation
source .venv-automation/bin/activate
pip install -r automation/requirements.txt
python -m playwright install --with-deps chromium
python automation/poshmark.py login
```

On Windows without WSL windows: install Python 3.11+ from python.org, then in PowerShell, from
the repo folder, run `py -m pip install -r automation\requirements.txt`, then
`py -m playwright install chromium`. Set
`$env:BROWSER_PROFILES = "$env:LOCALAPPDATA\storebot-profiles"` in each new window, and run the
same commands with `py` instead of `python`.

## Rules this folder follows

- It acts only on your own listings, and only after a recorded sale (or your typed `DELETE`).
- It moves at human speed, with 2 to 5 second random pauses, one listing at a time.
- It uses a visible window, your home internet, and no stealth tricks.
- It never posts or changes prices. Those still go through approvals.
