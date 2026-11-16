"""Poshmark safe test: log in once, then find the Delete button on a listing WITHOUT clicking it.

This is the first piece of the browser worker (see automation/README.md). It proves three
things before we ever let a script delete anything:
  1. A saved, logged-in browser profile works (you log in by hand once; no password is stored).
  2. The script can reach the edit page of one of your listings.
  3. It can find the Delete button there.

It CANNOT change your account:
  * It never clicks anything. It only opens pages by their address.
  * In `check` mode every request that could change data (POST, PUT, PATCH, DELETE) is
    blocked inside the browser before it leaves your computer. Even a mistake in this
    script, or a stray click by you in that window, can't save, edit, or delete anything.

Usage (from the repo folder):
  python automation/poshmark_check.py login              # opens a window: log in, then press Enter here
  python automation/poshmark_check.py check              # uses a live Poshmark listing from data/store.db
  python automation/poshmark_check.py check <listing-url>

Exit codes: 0 found the Delete button, 2 not logged in, 3 button not found, 1 other error.
Failures are also posted to #alerts if bot/.env has DISCORD_TOKEN and ALERTS_CHANNEL_ID.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Your login cookies live here (gitignored). BROWSER_PROFILES moves it, e.g. on native Windows.
PROFILE_DIR = Path(os.environ.get("BROWSER_PROFILES", ROOT / "browser-profiles")) / "poshmark"
SHOTS_DIR = ROOT / "data" / "automation"                 # screenshots land here
DB_PATH = ROOT / "data" / "store.db"
BASE = os.environ.get("POSHMARK_BASE", "https://poshmark.com")  # override only for offline tests

# Methods that can change something on the server. Blocked in check mode.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LISTING_ID = re.compile(r"([0-9a-f]{24})(?:[/?#]|$)")
DELETE_TEXT = re.compile(r"^\s*delete(\s+listing)?\s*$", re.I)


# ---------------------------------------------------------------- helpers
def human_pause(lo: float = 2.0, hi: float = 5.0) -> None:
    """Wait a random, human-like amount of time between actions."""
    time.sleep(random.uniform(lo, hi))


def load_env() -> None:
    env = ROOT / "bot" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def alert(text: str) -> None:
    """Post a failure to #alerts using the bot's own token. Silent if not configured."""
    token, channel = os.environ.get("DISCORD_TOKEN"), os.environ.get("ALERTS_CHANNEL_ID")
    if not token or not channel or "paste" in token:
        return
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=json.dumps({"content": text[:1900]}).encode(),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "storebot-automation (local, 1.0)"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # an alert failing must never hide the real result
        print(f"(could not post to Discord #alerts: {e})")


def listing_from_db() -> str | None:
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT url FROM listings WHERE platform_id='poshmark' AND status='live' "
        "AND url IS NOT NULL AND url <> '' ORDER BY posted_at DESC LIMIT 1").fetchone()
    con.close()
    return row[0] if row else None


def open_browser(pw, block_writes: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    headless = os.environ.get("POSHMARK_HEADLESS") == "1"   # only for offline tests
    kwargs = dict(headless=headless, viewport={"width": 1280, "height": 900})
    exe = os.environ.get("CHROMIUM_PATH")
    if exe:
        kwargs["executable_path"] = exe
    ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
    if block_writes:
        blocked = []

        def guard(route):
            if route.request.method in WRITE_METHODS:
                blocked.append(f"{route.request.method} {route.request.url[:80]}")
                return route.abort()
            return route.continue_()

        ctx.route("**/*", guard)
        ctx.blocked_writes = blocked  # type: ignore[attr-defined]
    return ctx


def logged_out(page) -> bool:
    url = page.url.lower()
    if "/login" in url or "/signup" in url:
        return True
    return page.get_by_role("link", name=re.compile(r"^\s*log ?in\s*$", re.I)).count() > 0


def screenshot(page, name: str) -> Path:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    return path


# ---------------------------------------------------------------- commands
def cmd_login() -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = open_browser(pw, block_writes=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/login")
        print("\nA browser window opened. Log in to Poshmark there like you normally would")
        print("(including any code they text or email you).")
        input("When you can see your Poshmark feed, come back here and press Enter... ")
        ok = not logged_out(page)
        ctx.close()
    print("Saved. You stay logged in for future runs." if ok
          else "It still looks logged out. Run `login` again.")
    return 0 if ok else 2


def cmd_check(url: str | None) -> int:
    from playwright.sync_api import sync_playwright
    url = url or listing_from_db()
    if not url:
        print("No listing given and no live Poshmark listing with a URL in data/store.db.")
        print("Run:  python automation/poshmark_check.py check https://poshmark.com/listing/...")
        return 1
    m = LISTING_ID.search(url)
    if not m:
        print(f"That doesn't look like a Poshmark listing link: {url}")
        return 1
    listing_id = m.group(1)
    url = url.split("?", 1)[0].split("#", 1)[0]   # drop ad/tracking junk after the ?

    with sync_playwright() as pw:
        ctx = open_browser(pw, block_writes=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            print(f"1/3 Opening the listing: {url}")
            page.goto(url, wait_until="domcontentloaded")
            human_pause()
            if logged_out(page):
                shot = screenshot(page, "poshmark-logged-out")
                msg = (f"Poshmark test: not logged in. Run `python automation/poshmark_check.py login`. "
                       f"Screenshot: {shot}")
                print(msg); alert(f"⚠️ {msg}")
                return 2

            # Open the edit page by address (a plain page load, not a click).
            edit = page.get_by_role("link", name=re.compile(r"edit( listing)?", re.I))
            href = edit.first.get_attribute("href") if edit.count() else None
            edit_url = (BASE + href) if href and href.startswith("/") else href
            edit_url = edit_url or f"{BASE}/edit-listing/{listing_id}"
            print(f"2/3 Opening the edit page: {edit_url}")
            page.goto(edit_url, wait_until="domcontentloaded")
            human_pause()
            if logged_out(page):
                shot = screenshot(page, "poshmark-logged-out")
                msg = f"Poshmark test: login expired on the edit page. Screenshot: {shot}"
                print(msg); alert(f"⚠️ {msg}")
                return 2

            if page.get_by_text(re.compile(r"page you are looking for could not be found", re.I)).count():
                shot = screenshot(page, "poshmark-not-yours")
                msg = (f"Poshmark test: the edit page doesn't exist for you (404). That's expected when "
                       f"the listing isn't on the logged-in account. Use one of your own listings. Screenshot: {shot}")
                print(msg); alert(f"⚠️ {msg}")
                return 3

            print("3/3 Looking for the Delete button (it will NOT be clicked)")
            btn = None
            for role in ("button", "link"):
                loc = page.get_by_role(role, name=DELETE_TEXT)
                if loc.count():
                    btn = loc.first
                    break
            if btn is None:
                loc = page.get_by_text(DELETE_TEXT)
                btn = loc.first if loc.count() else None

            if btn is None:
                shot = screenshot(page, "poshmark-no-delete")
                msg = (f"Poshmark test: could NOT find the Delete button on {edit_url}. "
                       f"Either the page changed or this listing isn't yours. Screenshot: {shot}")
                print(msg); alert(f"⚠️ {msg}")
                return 3

            btn.scroll_into_view_if_needed()
            # Draw a red box around it for the screenshot. This only changes how it looks
            # on your screen; nothing is sent to Poshmark.
            btn.evaluate("el => { el.style.outline = '4px solid red'; el.style.outlineOffset = '3px'; }")
            human_pause(1, 2)
            shot = screenshot(page, "poshmark-delete-found")
            label = (btn.inner_text() or "").strip()
            print(f"\nFOUND the Delete button (text: {label!r}). It was not clicked.")
            print(f"Screenshot: {shot}")
            blocked = getattr(ctx, "blocked_writes", [])
            print(f"Safety guard blocked {len(blocked)} write request(s) during the test"
                  " (page analytics, mostly). Nothing on your account changed.")
            human_pause(2, 3)
            return 0
        except Exception as e:
            shot = None
            try:
                shot = screenshot(page, "poshmark-error")
            except Exception:
                pass
            msg = f"Poshmark test failed with an error: {e!r}. Screenshot: {shot}"
            print(msg); alert(f"⚠️ {msg}")
            return 1
        finally:
            ctx.close()


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open a window and log in to Poshmark once")
    c = sub.add_parser("check", help="find the Delete button on a listing without clicking it")
    c.add_argument("url", nargs="?", help="Poshmark listing link (default: newest live one in the DB)")
    a = ap.parse_args()
    return cmd_login() if a.cmd == "login" else cmd_check(a.url)


if __name__ == "__main__":
    sys.exit(main())
