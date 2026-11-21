"""Selling chores: what to ship, nightly backups, and a yearly sales export.

    /toship                 sold items still to ship, oldest first, with bin and ship-by date
    /shipped sku            mark it shipped
    /export year            CSV of the year's sales: price, estimated fees, cost, net (for taxes)
    daily 8 AM (local)      #alerts: "to ship today" digest, @here when something is overdue
    nightly 3 AM (local)    copy of the database to data/backups/ (14 kept) and, if BACKUP_OFFSITE
                            is set, to that folder too, with any new photos. A failed backup
                            pings #alerts.
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import sys
import tomllib
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pricing"))
sys.path.insert(0, str(ROOT / "listing"))
sys.path.insert(0, str(ROOT / "sales"))
import fees as feecalc  # noqa: E402
import pricer  # noqa: E402
import sale_record as sr  # noqa: E402

CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())
OFF = CFG["goal"].get("tz_offset_hours", -7)
LOCAL = timezone(timedelta(hours=OFF))
DATA = ROOT / "data"
BACKUPS = DATA / "backups"
KEEP_BACKUPS = 14

db = None
bot = None
alerts_ch = 0


def _ch():
    return bot.get_channel(alerts_ch) if bot else None


# ------------------------------------------------------------ to ship
def to_ship(con: sqlite3.Connection) -> list[dict]:
    days = CFG.get("shipping", {}).get("ship_within_days", {})
    rows = con.execute("""
        SELECT i.sku, i.name, i.bin_id, l.platform_id, s.sold_at, s.sale_price
        FROM sales s JOIN items i ON i.id=s.item_id JOIN listings l ON l.id=s.listing_id
        WHERE s.voided_at IS NULL AND i.status='sold' ORDER BY s.sold_at""").fetchall()
    out = []
    today = datetime.now(LOCAL).date()
    for r in rows:
        if r["platform_id"] == "facebook":       # local pickup: nothing to ship
            continue
        sold = datetime.fromisoformat(r["sold_at"]).replace(tzinfo=timezone.utc).astimezone(LOCAL).date()
        by = sold + timedelta(days=int(days.get(r["platform_id"], 2)))
        out.append({**dict(r), "ship_by": by, "overdue": by < today, "due_today": by == today})
    return out


def ship_lines(items: list[dict]) -> list[str]:
    lines = []
    for x in items:
        flag = "🔴 OVERDUE" if x["overdue"] else ("🟠 today" if x["due_today"] else f"by {x['ship_by']:%a %b %d}")
        where = f"bin {x['bin_id']}" if x["bin_id"] is not None else "no bin"
        lines.append(f"• `{x['sku']}` {x['name'][:40]} · {sr.name(x['platform_id'])} · **{where}** · {flag}")
    return lines


async def toship(interaction: discord.Interaction):
    items = to_ship(db)
    if not items:
        await interaction.response.send_message("Nothing to ship. 🎉")
        return
    await interaction.response.send_message(
        (f"**📦 To ship ({len(items)})**\n" + "\n".join(ship_lines(items)) +
         "\nTap `/shipped` with the SKU once it's dropped off.")[:2000])


@app_commands.describe(sku="item SKU, e.g. S0007")
async def shipped(interaction: discord.Interaction, sku: str):
    it = sr.item_by_sku(db, sku)
    if not it or it["status"] != "sold":
        await interaction.response.send_message(f"{sku} isn't a sold item waiting to ship.")
        return
    db.execute("UPDATE items SET status='shipped', bin_id=NULL WHERE id=?", (it["id"],))
    db.commit()
    await interaction.response.send_message(f"✅ {it['sku']} shipped (and taken off bin {it['bin_id']})."
                                            if it["bin_id"] is not None else f"✅ {it['sku']} shipped.")


@tasks.loop(time=time(hour=(8 - OFF) % 24, tzinfo=timezone.utc))
async def ship_digest():
    items, ch = to_ship(db), _ch()
    if not items or ch is None:
        return
    late = any(x["overdue"] or x["due_today"] for x in items)
    await ch.send((("@here " if late else "") + f"**📦 To ship ({len(items)})**\n" + "\n".join(ship_lines(items)))[:2000],
                  allowed_mentions=discord.AllowedMentions(everyone=True))


# ------------------------------------------------------------ backups
def backup(con: sqlite3.Connection, when: date | None = None) -> list[Path]:
    """Copy the database (safe while the bot runs) and, if BACKUP_OFFSITE is set, photos too."""
    when = when or datetime.now(LOCAL).date()
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"store-{when:%Y-%m-%d}.db"
    out = sqlite3.connect(dest)
    try:
        con.backup(out)
    finally:
        out.close()
    for old in sorted(BACKUPS.glob("store-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    written = [dest]
    offsite = os.environ.get("BACKUP_OFFSITE", "").strip()
    if offsite:
        off = Path(offsite)
        if not off.is_dir():
            raise RuntimeError(f"BACKUP_OFFSITE folder {offsite} doesn't exist (is it mounted in docker-compose.yml?)")
        shutil.copy2(dest, off / dest.name)
        for old in sorted(off.glob("store-*.db"))[:-KEEP_BACKUPS]:
            old.unlink()
        photos = DATA / "photos"
        if photos.is_dir():
            (off / "photos").mkdir(exist_ok=True)
            for ph in photos.iterdir():
                target = off / "photos" / ph.name
                if ph.is_file() and not target.exists():
                    shutil.copy2(ph, target)
        written.append(off / dest.name)
    return written


@tasks.loop(time=time(hour=(3 - OFF) % 24, tzinfo=timezone.utc))
async def nightly_backup():
    try:
        backup(db)
    except Exception as e:  # noqa: BLE001  a silent backup failure is worse than a noisy one
        ch = _ch()
        print(f"Backup failed: {e}")
        if ch:
            await ch.send(f"@here 💾❌ Last night's backup failed: {type(e).__name__}: {e}",
                          allowed_mentions=discord.AllowedMentions(everyone=True))


# ------------------------------------------------------------ export
COLUMNS = ["sold_at", "sku", "name", "platform", "sale_price", "est_fees", "est_label_and_packaging",
           "cost_basis", "est_net", "source", "category"]


def sales_csv(con: sqlite3.Connection, year: int) -> tuple[str, int, float]:
    """Sales for the year (undone sales left out). Fees are estimated from fees.toml unless recorded."""
    rows = con.execute("""
        SELECT s.*, l.platform_id, i.sku, i.name, i.category, i.cost_basis, i.weight_oz
        FROM sales s JOIN listings l ON l.id=s.listing_id JOIN items i ON i.id=s.item_id
        WHERE s.voided_at IS NULL AND strftime('%Y', s.sold_at)=? ORDER BY s.sold_at""", (str(year),)).fetchall()
    table = feecalc.load_fees()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    total = 0.0
    for r in rows:
        cost = r["cost_basis"] or 0.0
        weight = (r["weight_oz"] or 16) / 16
        label = pricer.est_label(weight)
        try:
            b = feecalc.calc(r["platform_id"], r["sale_price"], category=r["category"] or "default",
                             label_cost=label, cost_basis=cost, weight_lb=weight, fees=table)
            local = any("local pickup" in n for n in b.notes)
            fees = r["fees"] if r["fees"] is not None else b.fees
            ship = b.label_cost - b.shipping_charged + (0 if local else CFG["pricing"]["packaging"]["usd"])
            net = r["net_profit"] if r["net_profit"] is not None else round(r["sale_price"] - fees - ship - cost, 2)
        except (KeyError, ValueError):
            fees, ship, net = r["fees"], None, r["net_profit"]
        total += r["sale_price"]
        w.writerow([r["sold_at"], r["sku"], r["name"], r["platform_id"], f"{r['sale_price']:.2f}",
                    "" if fees is None else f"{fees:.2f}", "" if ship is None else f"{ship:.2f}", f"{cost:.2f}",
                    "" if net is None else f"{net:.2f}", r["source"] or "", r["category"] or ""])
    return buf.getvalue(), len(rows), total


@app_commands.describe(year="e.g. 2026 (default: this year)")
async def export(interaction: discord.Interaction, year: int | None = None):
    year = year or datetime.now(LOCAL).year
    text, n, total = sales_csv(db, year)
    f = discord.File(io.BytesIO(text.encode()), filename=f"sales-{year}.csv")
    await interaction.response.send_message(
        f"🧾 {n} sale{'' if n == 1 else 's'} in {year}, ${total:,.2f} gross. Fees and net are estimates from the "
        f"fee table; check them against each site's annual report. Keep thrift receipts for the cost column.", file=f)


# ------------------------------------------------------------ wiring
def register(client, tree: app_commands.CommandTree, database, alerts_channel_id: int) -> None:
    global db, bot, alerts_ch
    db, bot, alerts_ch = database, client, alerts_channel_id
    tree.command(name="toship", description="Sold items still to ship, with bin and ship-by date")(toship)
    tree.command(name="shipped", description="Mark a sold item shipped")(shipped)
    tree.command(name="export", description="Download the year's sales as a CSV (for taxes)")(export)


def start() -> None:
    ship_digest.start()
    nightly_backup.start()
