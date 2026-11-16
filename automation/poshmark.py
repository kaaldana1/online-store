"""Poshmark browser worker: log in once, check, delist, and run the auto-delist worker.

Commands (run from the repo folder, inside .venv-automation):
  python automation/poshmark.py login                  open a window, log in by hand once
  python automation/poshmark.py check "<listing-url>"  find the Delete button, never click (safe)
  python automation/poshmark.py delist "<listing-url>" really delete one listing (asks you to type DELETE)
  python automation/poshmark.py worker                 watch the database and delist Poshmark listings
                                                       of items that sold somewhere else
  python automation/poshmark.py add-test-listing "<listing-url>"
                                                       add a TEST item for that listing to the database,
                                                       so you can try the whole sale -> delist flow

Safety, in every command:
  * A guard inside the browser blocks every request that could change data (POST, PUT, PATCH,
    DELETE). `delist` lifts it only for the few seconds between clicking "Delete Listing" and
    the confirm button, then turns it back on before checking the result.
  * Human pace: random 2-5 s pauses, one listing at a time, a visible window.
  * A delist only counts as done after the script reloads the edit page and Poshmark says the
    listing no longer exists. Anything else is a failure, and a failure always goes to #alerts.
  * The worker only acts on delist tasks created by a recorded sale, and only when bot/.env has
    AUTO_DELIST=poshmark. Remove that line to turn it off; delists go back to manual alerts.
  * CAPTCHA or login page = stop and alert. It never tries to get past them.

Exit codes: 0 ok, 2 not logged in, 3 button/listing not found or not verified, 1 other error.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Your login cookies live here (gitignored). BROWSER_PROFILES moves it, e.g. on native Windows.
PROFILE_DIR = Path(os.environ.get("BROWSER_PROFILES", ROOT / "browser-profiles")) / "poshmark"
SHOTS_DIR = ROOT / "data" / "automation"
DB_PATH = Path(os.environ.get("STORE_DB", ROOT / "data" / "store.db"))
BASE = os.environ.get("POSHMARK_BASE", "https://poshmark.com")  # override only for offline tests

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LISTING_ID = re.compile(r"([0-9a-f]{24})(?:[/?#]|$)")
FORM_TIMEOUT_MS = 30000
DELETE_TEXT = re.compile(r"^\s*delete(\s+listing)?\s*$", re.I)
CONFIRM_TEXT = re.compile(r"^\s*(yes|yes,? delete( it| listing)?|delete|confirm|ok)\s*$", re.I)
NOT_FOUND_TEXT = re.compile(r"page you are looking for could not be found|listing (is )?(no longer|not) available", re.I)
CAPTCHA_TEXT = re.compile(r"captcha|verify you are (a )?human|are you a robot", re.I)
POLL_SECONDS = 60


@dataclass
class Result:
    code: int            # 0 ok, 1 error, 2 logged out, 3 not found / not verified
    msg: str
    shot: Path | None = None

    @property
    def ok(self) -> bool:
        return self.code == 0


# ---------------------------------------------------------------- helpers
def human_pause(lo: float = 2.0, hi: float = 5.0) -> None:
    time.sleep(random.uniform(lo, hi))


def load_env() -> None:
    env = ROOT / "bot" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def alert(text: str, image: Path | None = None) -> None:
    """Post to #alerts with the bot's own token (optionally with a screenshot). Silent if not set up."""
    token, channel = os.environ.get("DISCORD_TOKEN"), os.environ.get("ALERTS_CHANNEL_ID")
    if not token or not channel or "paste" in token:
        return
    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    headers = {"Authorization": f"Bot {token}", "User-Agent": "storebot-automation (local, 1.0)"}
    payload = json.dumps({"content": text[:1900]}).encode()
    if image and image.exists():
        b = uuid.uuid4().hex
        ctype = mimetypes.guess_type(image.name)[0] or "image/png"
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                f"Content-Type: application/json\r\n\r\n").encode() + payload + (
                f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{image.name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + image.read_bytes() + f"\r\n--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    else:
        body = payload
        headers["Content-Type"] = "application/json"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"),
                               timeout=20).read()
    except Exception as e:  # an alert failing must never hide the real result
        print(f"(could not post to Discord #alerts: {e})")


def clean_url(url: str) -> tuple[str, str] | None:
    m = LISTING_ID.search(url or "")
    if not m:
        return None
    return url.split("?", 1)[0].split("#", 1)[0], m.group(1)


def open_browser(pw):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(headless=os.environ.get("POSHMARK_HEADLESS") == "1",
                  viewport={"width": 1280, "height": 900})
    if os.environ.get("CHROMIUM_PATH"):
        kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
    ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
    ctx.allow_writes = False   # type: ignore[attr-defined]
    ctx.blocked_writes = []    # type: ignore[attr-defined]

    def guard(route):
        if route.request.method in WRITE_METHODS and not ctx.allow_writes:
            ctx.blocked_writes.append(f"{route.request.method} {route.request.url[:200]}")
            return route.abort()
        return route.continue_()

    ctx.route("**/*", guard)
    return ctx


def logged_out(page) -> bool:
    url = page.url.lower()
    if "/login" in url or "/signup" in url:
        return True
    return page.get_by_role("link", name=re.compile(r"^\s*log ?in\s*$", re.I)).count() > 0


def captcha(page) -> bool:
    try:
        return page.get_by_text(CAPTCHA_TEXT).count() > 0 or page.locator("iframe[src*='captcha']").count() > 0
    except Exception:
        return False


def screenshot(page, name: str) -> Path | None:
    try:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        return path
    except Exception:
        return None


def settle(page, timeout: int = 15000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def delete_link(page):
    return (page.get_by_role("button", name=DELETE_TEXT)
            .or_(page.get_by_role("link", name=DELETE_TEXT))
            .or_(page.get_by_text(DELETE_TEXT))).first


# ---------------------------------------------------------------- the steps
def open_edit_page(page, url: str, listing_id: str):
    """Go listing -> edit page and wait for the Delete link. Returns (edit_url, locator, Result|None)."""
    print(f"  opening the listing: {url}")
    page.goto(url, wait_until="domcontentloaded")
    human_pause()
    if captcha(page):
        return None, None, Result(1, "Poshmark is showing a CAPTCHA / robot check. Stopped.", screenshot(page, "poshmark-captcha"))
    if logged_out(page):
        return None, None, Result(2, "Not logged in to Poshmark. Run `python automation/poshmark.py login`.",
                                  screenshot(page, "poshmark-logged-out"))
    if page.get_by_text(NOT_FOUND_TEXT).count():
        return None, None, Result(3, "The listing page doesn't exist (already deleted, or a wrong link).",
                                  screenshot(page, "poshmark-listing-404"))

    edit = page.get_by_role("link", name=re.compile(r"edit( listing)?", re.I))
    href = edit.first.get_attribute("href") if edit.count() else None
    edit_url = (BASE + href) if href and href.startswith("/") else (href or f"{BASE}/edit-listing/{listing_id}")
    print(f"  opening the edit page: {edit_url}")
    page.goto(edit_url, wait_until="domcontentloaded")
    human_pause()
    if logged_out(page):
        return edit_url, None, Result(2, "Poshmark login expired on the edit page. Run `login` again.",
                                      screenshot(page, "poshmark-logged-out"))
    if page.get_by_text(NOT_FOUND_TEXT).count():
        return edit_url, None, Result(3, "The edit page doesn't exist for this account (404). The listing "
                                         "isn't yours, or it's already gone.", screenshot(page, "poshmark-not-yours"))
    print("  waiting for the edit form to load")
    settle(page)
    btn = delete_link(page)
    try:
        btn.wait_for(state="attached", timeout=FORM_TIMEOUT_MS)
    except Exception:
        return edit_url, None, Result(3, f"Couldn't find the Delete Listing button on {edit_url} after "
                                         f"{FORM_TIMEOUT_MS // 1000}s. The page may have changed.",
                                      screenshot(page, "poshmark-no-delete"))
    return edit_url, btn, None


def verify_gone(page, edit_url: str) -> Result:
    """Reload the edit page with the guard on. Only a Poshmark 'not found' counts as deleted."""
    print("  checking that it's really gone")
    human_pause()
    page.goto(edit_url, wait_until="domcontentloaded")
    settle(page)
    deadline = time.time() + FORM_TIMEOUT_MS / 1000
    while time.time() < deadline:
        if page.get_by_text(NOT_FOUND_TEXT).count():
            return Result(0, "Deleted and verified: Poshmark says the listing no longer exists.",
                          screenshot(page, "poshmark-delist-verified"))
        if delete_link(page).count():
            return Result(3, "Clicked delete, but the listing is STILL THERE on Poshmark.",
                          screenshot(page, "poshmark-still-there"))
        time.sleep(1)
    return Result(3, "Clicked delete, but couldn't confirm it's gone (page never finished loading).",
                  screenshot(page, "poshmark-unverified"))


def do_check(page, url: str, listing_id: str) -> Result:
    edit_url, btn, err = open_edit_page(page, url, listing_id)
    if err:
        return err
    btn.scroll_into_view_if_needed()
    btn.evaluate("el => { el.style.outline = '4px solid red'; el.style.outlineOffset = '3px'; }")
    human_pause(1, 2)
    return Result(0, "Found the Delete Listing button. It was not clicked.", screenshot(page, "poshmark-delete-found"))


def do_delist(ctx, page, url: str, listing_id: str) -> Result:
    edit_url, btn, err = open_edit_page(page, url, listing_id)
    if err:
        return err
    native = {"seen": False}

    def on_dialog(d):  # in case Poshmark uses a browser confirm() box instead of its own popup
        native["seen"] = True
        d.accept()

    btn.scroll_into_view_if_needed()
    human_pause(1, 3)
    print("  clicking Delete Listing")
    ctx.allow_writes = True
    page.on("dialog", on_dialog)
    try:
        btn.click()
        confirm = None
        deadline = time.time() + 10
        while time.time() < deadline and not native["seen"]:
            loc = page.get_by_role("button", name=CONFIRM_TEXT)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    confirm = loc.nth(i)
                    break
            if confirm:
                break
            time.sleep(0.5)
        if confirm is None and not native["seen"]:
            ctx.allow_writes = False
            return Result(3, "Clicked Delete Listing, but no confirm button appeared, so nothing was deleted. "
                             "The page may have changed.", screenshot(page, "poshmark-no-confirm"))
        if confirm is not None:
            human_pause(1, 2)
            print(f"  confirming ({(confirm.inner_text() or '').strip()!r})")
            confirm.click()
        settle(page, 20000)
        human_pause(2, 3)
    finally:
        ctx.allow_writes = False
        page.remove_listener("dialog", on_dialog)
    return verify_gone(page, edit_url)


def run(action: str, url: str) -> Result:
    from playwright.sync_api import sync_playwright
    parsed = clean_url(url)
    if not parsed:
        return Result(1, f"That doesn't look like a Poshmark listing link: {url}")
    url, listing_id = parsed
    with sync_playwright() as pw:
        try:
            ctx = open_browser(pw)
        except Exception as e:
            hint = (" Is another copy running with the same login (the worker container, or `login`)? "
                    "Only one browser can use the saved login at a time." if "lock" in str(e).lower()
                    or "in use" in str(e).lower() else "")
            return Result(1, f"Couldn't start the browser: {str(e)[:300]}.{hint}")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            return do_check(page, url, listing_id) if action == "check" else do_delist(ctx, page, url, listing_id)
        except Exception as e:
            return Result(1, f"Error: {e!r}", screenshot(page, "poshmark-error"))
        finally:
            human_pause(1, 2)
            ctx.close()


def report(r: Result, what: str, url: str, notify_ok: bool = False) -> None:
    print(("\nOK: " if r.ok else "\nFAILED: ") + r.msg)
    if r.shot:
        print(f"Screenshot: {r.shot}")
    if not r.ok:
        alert(f"⚠️ Poshmark {what} FAILED for {url}\n{r.msg}\nPlease check it by hand.", r.shot)
    elif notify_ok:
        alert(f"✅ Poshmark {what}: {url}\n{r.msg}", r.shot)


# ---------------------------------------------------------------- database
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def auto_delist_on() -> bool:
    return "poshmark" in {p.strip().lower() for p in os.environ.get("AUTO_DELIST", "").split(",")}


def idle(msg: str) -> int:
    """In Docker: explain once, then wait instead of exiting (exiting makes Docker restart us in a loop)."""
    print(msg + "\nWaiting. Fix it, then restart:  docker compose restart poshmark-worker")
    while True:
        time.sleep(3600)


def single_worker_lock(once: bool):
    """Only one worker at a time (e.g. the Docker one and one in a terminal), so a listing is never
    handled twice. Works across Docker and this computer because both see the same data/ folder."""
    try:
        import fcntl
    except ImportError:          # native Windows: no lock, just don't run two copies
        return True
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    f = open(SHOTS_DIR / "poshmark-worker.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if once:
            print("Another Poshmark worker is already running (maybe the Docker one), so this one will "
                  "not run. Stop the other one first:  docker compose stop poshmark-worker")
            return None
        print("Another Poshmark worker is already running. This one will wait and take over when it stops.")
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


def worker(once: bool, idle_if_off: bool = False) -> int:
    if not DB_PATH.exists():
        msg = f"No database at {DB_PATH}. Start the bot once first."
        return idle(msg) if idle_if_off else (print(msg) or 1)
    if not auto_delist_on():
        msg = ("Auto-delist is off. Add this line to bot/.env, then restart the bot and this worker:\n"
               "    AUTO_DELIST=poshmark")
        return idle(msg) if idle_if_off else (print(msg) or 1)
    lock = single_worker_lock(once)
    if lock is None:
        return 1
    db_alerted = False
    print(f"Watching for Poshmark delists every {POLL_SECONDS}s. Leave this window open; Ctrl+C to stop.")
    while True:
        try:
            con = db()
            rows = con.execute("""
                SELECT d.id, d.attempts, l.id AS listing_id, l.url, i.sku
                FROM delist_tasks d JOIN listings l ON l.id = d.listing_id JOIN items i ON i.id = l.item_id
                WHERE d.status = 'pending' AND l.platform_id = 'poshmark' AND d.reason_sale IS NOT NULL
                ORDER BY d.id""").fetchall()
            for t in rows:
                # Claim it. If another copy of the worker got there first, skip it.
                claimed = con.execute("UPDATE delist_tasks SET attempts = attempts + 1 "
                                      "WHERE id=? AND status='pending' AND attempts=?",
                                      (t["id"], t["attempts"])).rowcount
                con.commit()
                if not claimed:
                    continue
                print(f"\n[{datetime.now():%H:%M}] {t['sku']}: sold elsewhere, delisting from Poshmark")
                if not t["url"]:
                    r = Result(3, "No Poshmark link is saved for this listing, so it can't be found automatically.")
                else:
                    try:
                        r = run("delist", t["url"])
                    except Exception as e:  # never let one listing stop the worker
                        r = Result(1, f"Unexpected error: {e!r}")
                report(r, f"delist of {t['sku']}", t["url"] or "(no link)", notify_ok=True)
                if r.ok:
                    con.execute("UPDATE delist_tasks SET status='done', done_at=datetime('now'), last_error=NULL "
                                "WHERE id=?", (t["id"],))
                    con.execute("UPDATE listings SET status='ended', ended_at=datetime('now') WHERE id=?",
                                (t["listing_id"],))
                elif r.code == 1 and t["attempts"] < 1:
                    # one retry, a minute later, for a plain error (timeout, network)
                    con.execute("UPDATE delist_tasks SET last_error=? WHERE id=?", (r.msg[:500], t["id"]))
                else:
                    # the bot shows this in #alerts with a "Done, I delisted it" button and re-pings
                    con.execute("UPDATE delist_tasks SET status='failed', last_error=? WHERE id=?",
                                (r.msg[:500], t["id"]))
                    con.execute("UPDATE listings SET status='delist_failed' WHERE id=?", (t["listing_id"],))
                con.commit()
                human_pause(5, 10)
            con.close()
            db_alerted = False
        except sqlite3.OperationalError as e:
            msg = f"Database problem: {e}."
            if "readonly" in str(e):
                msg += " Fix once with:  sudo chown -R $USER:$USER data"
            print(msg)
            if not db_alerted:  # once, not every minute
                alert(f"⚠️ Poshmark worker: {msg} Delists are falling back to manual alerts.")
                db_alerted = True
            if once:
                return 1
        if once:
            return 0
        time.sleep(POLL_SECONDS)


def add_test_listing(url: str) -> int:
    parsed = clean_url(url)
    if not parsed:
        print(f"That doesn't look like a Poshmark listing link: {url}")
        return 1
    url, _ = parsed
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Start the bot once first.")
        return 1
    con = db()
    n = con.execute("SELECT COUNT(*) FROM items WHERE sku LIKE 'TEST-PM%'").fetchone()[0] + 1
    sku = f"TEST-PM{n}"
    item_id = con.execute("INSERT INTO items (sku, name, status) VALUES (?, ?, 'listed')",
                          (sku, "Poshmark auto-delist test")).lastrowid
    con.execute("INSERT INTO listings (item_id, platform_id, status, url, posted_at) "
                "VALUES (?, 'poshmark', 'live', ?, datetime('now'))", (item_id, url))
    con.commit()
    print(f"Added {sku}, linked to {url}\n"
          f"Now, with the worker running, type this in Discord to fake a sale on Facebook:\n"
          f"    /sold sku:{sku} platform:facebook price:1")
    return 0


# ---------------------------------------------------------------- CLI
def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open a window and log in to Poshmark once")
    sub.add_parser("check", help="find the Delete button without clicking it").add_argument("url")
    d = sub.add_parser("delist", help="really delete one listing")
    d.add_argument("url")
    d.add_argument("--yes", action="store_true", help="skip the 'type DELETE' question")
    w = sub.add_parser("worker", help="auto-delist Poshmark listings of items sold elsewhere")
    w.add_argument("--once", action="store_true", help="handle what's waiting, then exit")
    w.add_argument("--idle-if-off", action="store_true", help="(Docker) wait instead of exiting when off")
    sub.add_parser("add-test-listing", help="add a TEST item for a listing link").add_argument("url")
    a = ap.parse_args()

    if a.cmd == "login":
        return login()
    if a.cmd == "check":
        r = run("check", a.url)
        report(r, "check", a.url)
        return r.code
    if a.cmd == "delist":
        if not a.yes:
            print(f"This will PERMANENTLY delete this Poshmark listing:\n    {a.url}")
            if input("Type DELETE to continue: ").strip() != "DELETE":
                print("Cancelled. Nothing was changed.")
                return 1
        r = run("delist", a.url)
        report(r, "delist", a.url, notify_ok=True)
        return r.code
    if a.cmd == "worker":
        try:
            return worker(a.once, a.idle_if_off)
        except KeyboardInterrupt:
            print("\nStopped. Delists will show up as manual alerts in #alerts until you start it again.")
            return 0
    return add_test_listing(a.url)


def login() -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = open_browser(pw)
        ctx.allow_writes = True   # logging in has to send your details
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/login")
        print("\nA browser window opened. Log in to Poshmark there like you normally would")
        print("(including any code they text or email you).")
        input("When you can see your Poshmark feed, come back here and press Enter... ")
        ok = not logged_out(page)
        ctx.close()
    print("Saved. You stay logged in for future runs." if ok else "It still looks logged out. Run `login` again.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
