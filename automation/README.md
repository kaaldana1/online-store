# Browser automation (step 1: safe Poshmark test)

This folder will grow into the browser worker that delists sold items on Poshmark, Mercari and
Depop. The design and the reasoning are in the
[recommendation doc](https://claude.ai/artifact/1tMahX8ePp9NNmYzCVW8UE).

Right now it holds one script, `poshmark_check.py`, which does a **dry run**:

1. You log in to Poshmark once in a browser window the script opens. Your password is never
   saved. Only the browser's login cookies are kept, in `browser-profiles/poshmark/`.
2. The script opens one of your listings, then its edit page, and finds the **Delete** button.
3. It draws a red box around the button, takes a screenshot, and closes. **It never clicks
   anything.**

**It can't change your account, even by accident.** While `check` runs, the browser blocks every
request that could save or delete something (POST, PUT, PATCH, DELETE) before it leaves your
computer. Even if you click something in that window yourself, the request won't go through.
When the test finishes it prints how many requests the guard blocked. Expect a few, because
page analytics also send POST requests.

The script runs on your PC, not in Docker, because it needs a visible browser window.

## Option A: run it in WSL (recommended)

You need Windows 11, or Windows 10 with the latest WSL, so that Linux apps can open windows.
If you're unsure, run `wsl --update` in PowerShell first.

```bash
cd ~/store-system
git pull

# one-time setup (about 2 minutes)
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv .venv-automation
source .venv-automation/bin/activate
pip install -r automation/requirements.txt
python -m playwright install --with-deps chromium      # asks for your sudo password

# log in once (a Chromium window opens)
python automation/poshmark_check.py login

# the test: paste the link of a listing on YOUR account
python automation/poshmark_check.py check "https://poshmark.com/listing/..."
```

**Always put the link in quotes.** Links copied from ads or the app often have `&` in them,
and without quotes the terminal splits the link into pieces and runs them in the background.

Later runs only need `source .venv-automation/bin/activate`, followed by the `check` line.
If you leave out the link, the script uses the newest live Poshmark listing in `data/store.db`
that has a URL.

If no window appears, run `wsl --update` in PowerShell, restart WSL with `wsl --shutdown`, and
try again. If it still doesn't appear, use Option B.

## Option B: run it on Windows directly

Install Python 3.11+ from python.org and tick "Add to PATH". Then, in PowerShell:

```powershell
cd \\wsl.localhost\Ubuntu\home\<your-linux-user>\store-system
py -m pip install -r automation\requirements.txt
py -m playwright install chromium
$env:BROWSER_PROFILES = "$env:LOCALAPPDATA\storebot-profiles"   # keep the login on Windows' own disk
py automation\poshmark_check.py login
py automation\poshmark_check.py check "https://poshmark.com/listing/..."
```

Set `BROWSER_PROFILES` the same way in every new PowerShell window. If you don't, the script
can't find your saved login.

## What the results mean

| Exit | Prints | Meaning |
|---|---|---|
| 0 | `FOUND the Delete button` | It works. The screenshot in `data/automation/` shows the red box. |
| 2 | `not logged in` | Run `login` again. Poshmark sometimes ends sessions. |
| 3 | `could NOT find the Delete button` | Either the listing isn't on the logged-in account, or Poshmark changed its page. Send the screenshot. |
| 1 | `failed with an error` | Anything else, such as a timeout or a bad link. Send the message and screenshot. |

Failures are also posted to `#alerts` using the bot's token, if `bot/.env` has `DISCORD_TOKEN`
and `ALERTS_CHANNEL_ID`.

**No listings yet?** You can still run `login` and then `check` on any public Poshmark listing.
It should end with exit 3 and say the edit page doesn't exist (404), because the listing isn't yours. That shows the login and page loading
work. Run the full test once your first item is posted.

## Safety rules this folder follows

- It acts only on your own listings and never browses or scrapes other sellers.
- It moves at human speed, with 2 to 5 second random pauses between pages.
- It uses a visible window, your home internet, and no stealth tricks.
- If it sees a CAPTCHA or a login prompt, it stops and alerts you. It never tries to get past them.
- Real deleting will come in a later PR, and only after you say so.
