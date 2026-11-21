"""Discord side of the sale email watcher (sales/email_watch.py).

Every few minutes: read new marketplace emails (read-only), and for each sale email
  * strong match (SKU or listing link) + auto_record on → record the sale, post a 💰 @here
    message, and the usual delist alerts follow
  * otherwise → a card in #alerts with "Yes, it sold" / "Not a sale" (@here)
  * no match → an @here card with the email subject and "Handled it"
  * already sold elsewhere → an @here "possible double sale" card
Cards that nobody has tapped are re-posted with @here every 2 hours.

If the inbox can't be read (bad password, network, Gmail down) #alerts gets an @here right
away and again every 2 hours until it works, so a silent watcher can never be mistaken for
"no sales".
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import tasks

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sales"))
import email_watch as ew  # noqa: E402
import sale_record as sr  # noqa: E402

REPING_EVERY = timedelta(hours=2)
OPEN_STATES = ("needs_confirm", "unmatched", "double_sale")

db = None
bot = None
alerts_ch = 0
rules = ew.load_rules()
state = {"failing_since": None, "last_fail_ping": None, "last_ok": None, "error": None}
last_ping: dict[int, datetime] = {}


def _ch():
    return bot.get_channel(alerts_ch) if bot else None


def _kv(key: str, value: str | None = None) -> str | None:
    if value is not None:
        db.execute("INSERT INTO bot_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))
        db.commit()
        return value
    r = db.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _since() -> datetime:
    now = datetime.now(timezone.utc)
    first = _kv("email_watch_started") or _kv("email_watch_started", (now - timedelta(days=1)).isoformat())
    look = now - timedelta(days=int(rules["settings"].get("lookback_days", 3)))
    return max(datetime.fromisoformat(first), look)


def _seen() -> set[str]:
    return {r[0] for r in db.execute("SELECT message_id FROM email_events WHERE created_at > datetime('now','-60 days')")}


# ------------------------------------------------------------ buttons
class EmailSaleButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"emailsale:(?P<action>yes|no|done):(?P<id>\d+)"):
    LABELS = {"yes": ("Yes, it sold: record it", discord.ButtonStyle.danger),
              "no": ("Not a sale", discord.ButtonStyle.secondary),
              "done": ("Handled it", discord.ButtonStyle.primary)}

    def __init__(self, action: str, event_id: int):
        self.action, self.event_id = action, event_id
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"emailsale:{action}:{event_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        ev = db.execute("SELECT * FROM email_events WHERE id=?", (self.event_id,)).fetchone()
        if ev is None or ev["status"] not in OPEN_STATES:
            await interaction.response.send_message(f"Already handled ({ev['status'] if ev else 'gone'}).",
                                                    ephemeral=True)
            return
        who = interaction.user.display_name
        if self.action in ("no", "done"):
            db.execute("UPDATE email_events SET status='dismissed', note=? WHERE id=?", (f"by {who}", ev["id"]))
            db.commit()
            await interaction.response.edit_message(
                content=f"~~{interaction.message.content or ''}~~ {'Not a sale' if self.action == 'no' else 'Handled'}"
                        f" (by {who})"[:2000], view=None)
            return
        try:
            res = sr.record_sale(db, ev["item_id"], ev["platform_id"], ev["sale_price"] or 0.0, source="email")
        except sr.AlreadySold as e:
            db.execute("UPDATE email_events SET status='dismissed', note=? WHERE id=?", (str(e), ev["id"]))
            db.commit()
            await interaction.response.edit_message(content=f"{e} Nothing new recorded.", view=None)
            return
        db.execute("UPDATE email_events SET status='confirmed', sale_id=?, note=? WHERE id=?",
                   (res.sale_id, f"by {who}", ev["id"]))
        db.commit()
        await interaction.response.edit_message(content=_sold_text(res, f"confirmed by {who}"), view=None)
        await bot.post_delist_alerts()


def _view(*buttons: tuple[str, int]) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for action, eid in buttons:
        v.add_item(EmailSaleButton(action, eid))
    return v


def _sold_text(res: sr.SaleResult, how: str) -> str:
    it = res.item
    where = f" It's in **bin {it['bin_id']}**." if it["bin_id"] is not None else ""
    msg = f"💰 **{it['sku']}** sold on **{sr.name(res.platform)}** for ${res.price:.2f} ({how}).{where}"
    if res.to_remove:
        msg += (f"\n⚠️ Remove it from **{', '.join(sr.name(p) for p in res.to_remove)}** now. "
                f"Delist alerts are below; tap Done on each.")
    else:
        msg += " Not listed anywhere else."
    return msg + f"\nWrong? `/unsold {it['sku']}` puts it back on sale."


# ------------------------------------------------------------ one email
async def handle(e: ew.Email) -> None:
    ch = _ch()
    if not e.platform:
        db.execute("INSERT OR IGNORE INTO email_events (message_id, subject, received_at, status) "
                   "VALUES (?,?,?,'ignored')", (e.message_id, e.subject, e.received_at))
        db.commit()
        return
    m = ew.match(db, e)
    price = ew.find_price(e)
    pname = sr.name(e.platform)
    subj = e.subject[:200]

    def save(status, note=None, sale_id=None):
        cur = db.execute("INSERT OR IGNORE INTO email_events (message_id, platform_id, subject, received_at, status, "
                         "match_how, item_id, sale_price, sale_id, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (e.message_id, e.platform, e.subject, e.received_at, status, m.how,
                          m.item["id"] if m.item else None, price if price is not None else
                          (_list_price(m.item["id"], e.platform) if m.item else None), sale_id, note))
        db.commit()
        return cur.lastrowid

    if m.item is None:
        eid = save("unmatched", f"candidates: {', '.join(m.candidates)}" if m.candidates else None)
        hint = f" It could be {', '.join(m.candidates)}." if m.candidates else ""
        if ch:
            await ch.send(f"@here 📧 A **{pname}** sale email came in that I couldn't match to an item: “{subj}”.{hint}\n"
                          f"Record it with `/sold <sku> {e.platform} <price>` so the other listings get removed, "
                          f"then tap Handled it.", view=_view(("done", eid)),
                          allowed_mentions=discord.AllowedMentions(everyone=True))
        return

    prior = sr.active_sale(db, m.item["id"])
    if prior is not None:
        if prior["platform_id"] == e.platform:
            save("duplicate", f"sale #{prior['id']} already recorded")
            return
        eid = save("double_sale", f"already sold on {prior['platform_id']}")
        if ch:
            await ch.send(f"@here 🚨 **Possible double sale:** {pname} says **{m.item['sku']}** "
                          f"({m.item['name']}) sold, but it's already recorded as sold on "
                          f"**{sr.name(prior['platform_id'])}**. Check both orders before shipping and cancel one.\n"
                          f"Email: “{subj}”", view=_view(("done", eid)),
                          allowed_mentions=discord.AllowedMentions(everyone=True))
        return

    sale_price = price if price is not None else (_list_price(m.item["id"], e.platform) or 0.0)
    if m.strong and rules["settings"].get("auto_record", False):
        res = sr.record_sale(db, m.item["id"], e.platform, sale_price, source="email")
        save("recorded", sale_id=res.sale_id)
        if ch:
            await ch.send(("@here " if res.to_remove else "") + _sold_text(res, f"from a {pname} email"),
                          allowed_mentions=discord.AllowedMentions(everyone=True))
        await bot.post_delist_alerts()
        return

    eid = save("needs_confirm")
    others = [r["platform_id"] for r in db.execute(
        "SELECT platform_id FROM listings WHERE item_id=? AND platform_id<>? AND status IN ('live','approved')",
        (m.item["id"], e.platform))]
    how = {"sku": "the SKU is in the email", "link": "the email links to the listing",
           "title": "matched by title only, so please double-check"}[m.how]
    also = (f"\nIt's also listed on **{', '.join(sr.name(p) for p in others)}**. Tap Yes and you'll get delist "
            f"alerts for those." if others else "")
    if ch:
        await ch.send(f"@here 📧 Did **{m.item['sku']}** ({m.item['name']}) sell on **{pname}** for "
                      f"{'$%.2f' % sale_price if sale_price else 'an unknown price'}? ({how})\nEmail: “{subj}”{also}",
                      view=_view(("yes", eid), ("no", eid)), allowed_mentions=discord.AllowedMentions(everyone=True))


def _list_price(item_id: int, platform: str) -> float | None:
    r = db.execute("SELECT list_price FROM listings WHERE item_id=? AND platform_id=?", (item_id, platform)).fetchone()
    return r["list_price"] if r else None


# ------------------------------------------------------------ loop + health
async def _fail(err: str) -> None:
    now = datetime.now(timezone.utc)
    first = state["failing_since"] is None
    state["failing_since"] = state["failing_since"] or now
    state["error"] = err
    print(f"Email watcher error: {err}")
    if first or (state["last_fail_ping"] and now - state["last_fail_ping"] >= REPING_EVERY):
        ch = _ch()
        if ch:
            await ch.send(f"@here 📧❌ **Can't check the sales inbox:** {err[:300]}\nUntil this is fixed, sales are "
                          f"NOT detected automatically. Record each sale with `/sold`. (I'll re-ping every 2 hours.)",
                          allowed_mentions=discord.AllowedMentions(everyone=True))
        state["last_fail_ping"] = now


_lock = asyncio.Lock()


async def poll_once() -> int:
    """Check the inbox once. Returns how many new marketplace emails were handled, or -1 on failure."""
    async with _lock:          # /inbox and the timer must never handle the same email twice
        return await _poll()


async def _poll() -> int:
    cfg = ew.ImapConfig.from_env()
    since, seen = _since(), _seen()
    try:
        emails = await asyncio.wait_for(asyncio.to_thread(ew.fetch_emails, cfg, rules, since, seen), timeout=180)
    except Exception as e:  # noqa: BLE001  any failure must reach the seller
        await _fail(f"{type(e).__name__}: {e}")
        return -1
    if state["failing_since"] is not None:
        ch = _ch()
        if ch:
            await ch.send("✅ The sales inbox is readable again. Automatic sale detection is back on.")
        state.update(failing_since=None, last_fail_ping=None, error=None)
    state["last_ok"] = datetime.now(timezone.utc)
    for e in emails:
        if db.execute("SELECT 1 FROM email_events WHERE message_id=?", (e.message_id,)).fetchone():
            continue
        try:
            await handle(e)
        except Exception as ex:  # noqa: BLE001
            await _fail(f"couldn't process the email “{e.subject[:80]}”: {type(ex).__name__}: {ex}")
    return len(emails)


async def reping_open_cards() -> None:
    ch = _ch()
    if ch is None:
        return
    now = datetime.now(timezone.utc)
    for ev in db.execute("SELECT e.*, i.sku FROM email_events e LEFT JOIN items i ON i.id=e.item_id "
                         f"WHERE e.status IN {OPEN_STATES} AND e.created_at < datetime('now','-2 hours')").fetchall():
        last = last_ping.get(ev["id"])
        if last and now - last < REPING_EVERY:
            continue
        last_ping[ev["id"]] = now
        what = {"needs_confirm": f"Did **{ev['sku']}** sell on {sr.name(ev['platform_id'])}? Nobody has answered yet.",
                "unmatched": f"Unmatched {sr.name(ev['platform_id'])} sale email still open. Record it with `/sold`.",
                "double_sale": f"Possible double sale of **{ev['sku']}** still open."}[ev["status"]]
        buttons = [("yes", ev["id"]), ("no", ev["id"])] if ev["status"] == "needs_confirm" else [("done", ev["id"])]
        await ch.send(f"@here ⏰ {what}\nEmail: “{(ev['subject'] or '')[:200]}”", view=_view(*buttons),
                      allowed_mentions=discord.AllowedMentions(everyone=True))


@tasks.loop(minutes=1)
async def watch_loop():
    minutes = int(rules["settings"].get("poll_minutes", 3))
    tick = watch_loop.current_loop
    if tick % max(minutes, 1) == 0:
        await poll_once()
    await reping_open_cards()


@watch_loop.before_loop
async def _before():
    await bot.wait_until_ready()


async def _announce_off():
    await bot.wait_until_ready()
    print("Email sale watcher OFF: set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in bot/.env")
    ch = _ch()
    if ch:
        await ch.send("📧 Automatic sale detection is **off** (no EMAIL_ADDRESS / EMAIL_APP_PASSWORD in bot/.env). "
                      "Record every sale with `/sold`.")


def status_line() -> str:
    if ew.ImapConfig.from_env() is None:
        return "📧 Sale emails: off (use /sold)"
    if state["failing_since"]:
        return f"📧 Sale emails: ❌ failing since {state['failing_since']:%H:%M} UTC ({state['error'][:80]})"
    if state["last_ok"]:
        return f"📧 Sale emails: ✅ last checked {state['last_ok']:%H:%M} UTC"
    return "📧 Sale emails: starting"


def register(client, database, alerts_channel_id: int) -> None:
    global db, bot, alerts_ch
    db, bot, alerts_ch = database, client, alerts_channel_id
    client.add_dynamic_items(EmailSaleButton)


def start() -> None:
    """Call from setup_hook (needs the running event loop)."""
    if ew.ImapConfig.from_env() is None:
        asyncio.get_running_loop().create_task(_announce_off())
    else:
        watch_loop.start()
