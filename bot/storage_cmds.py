"""Step 3 Discord commands: /find, /bin, /move, /sold.

/sold is the cross-platform safety net: recording a sale marks it sold and the database
creates a delist task for every OTHER live listing of that item. Those show up in #alerts
(with re-pings) until someone confirms each one was taken down.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "storage"))
import search  # noqa: E402

STORAGE_URL = os.environ.get("STORAGE_URL", "http://localhost:8765")
NAMES = {"ebay": "eBay", "poshmark": "Poshmark", "facebook": "Facebook"}
# Platforms with a working automatic-delist worker, from AUTO_DELIST in bot/.env (e.g.
# AUTO_DELIST=poshmark). Poshmark's worker is automation/poshmark.py and runs on the PC.
# Anything not listed stays a human task, so nothing can slip through. If a worker is off or
# stuck, the bot still alerts after 10 minutes.
def auto_delist() -> set[str]:
    return {p.strip().lower() for p in os.environ.get("AUTO_DELIST", "").split(",") if p.strip()}
db = None
bot = None


def _item_by_sku(sku: str):
    return db.execute("SELECT * FROM items WHERE upper(sku)=upper(?)", (sku.strip(),)).fetchone()


def _photo_file(item_id: int) -> discord.File | None:
    r = db.execute("SELECT path FROM item_photos WHERE item_id=? ORDER BY is_primary DESC, id LIMIT 1",
                   (item_id,)).fetchone()
    if r and (ROOT / r["path"]).is_file():
        return discord.File(ROOT / r["path"], filename=Path(r["path"]).name)
    return None


@app_commands.describe(description="what it looks like, e.g. golden chalice with vine engravings",
                       smart="let AI pick the best match (fractions of a cent)")
async def find(interaction: discord.Interaction, description: str, smart: bool = True):
    await interaction.response.defer(thinking=True)
    if smart:
        from init_db import connect
        res = await asyncio.to_thread(lambda: search.smart_find(connect(), description, limit=3))
    else:
        res = search.find(db, description, limit=3)
    if not res:
        await interaction.followup.send(f"Nothing matched “{description}”. Try different words.")
        return
    embeds, files = [], []
    for r in res:
        where = f"**Bin {r['bin_id']}**" if r["bin_id"] is not None else "not in a bin yet"
        e = discord.Embed(title=f"{r['sku']} · {r['name']}"[:256], description=where +
                          (f"\n{r['why']} ({r['confidence']})" if r.get("why") else ""))
        if r["bin_id"] is not None:
            e.url = f"{STORAGE_URL}/?highlight={r['bin_id']}"
        f = _photo_file(r["id"])
        if f:
            e.set_thumbnail(url=f"attachment://{f.filename}")
            files.append(f)
        embeds.append(e)
    await interaction.followup.send(f"Best match{'es' if len(res) > 1 else ''} for “{description}”:",
                                    embeds=embeds, files=files)


@app_commands.describe(number="bin number")
async def bin_(interaction: discord.Interaction, number: int):
    b = db.execute("SELECT * FROM bins WHERE id=?", (number,)).fetchone()
    if not b:
        await interaction.response.send_message(f"There's no bin {number} on the map yet.")
        return
    rows = db.execute(f"SELECT sku, name FROM items WHERE bin_id=? AND status IN {search.ACTIVE} ORDER BY id",
                      (number,)).fetchall()
    body = "\n".join(f"• `{r['sku']}` {r['name']}" for r in rows) or "Empty."
    await interaction.response.send_message(f"**Bin {number}**{' · ' + b['label'] if b['label'] else ''} "
                                            f"({len(rows)} item{'' if len(rows) == 1 else 's'})\n{body}"[:2000])


@app_commands.describe(sku="item SKU, e.g. S0007", number="bin number (0 = take it out of its bin)")
async def move(interaction: discord.Interaction, sku: str, number: int):
    it = _item_by_sku(sku)
    if not it:
        await interaction.response.send_message(f"No item {sku}.")
        return
    if number == 0:
        db.execute("UPDATE items SET bin_id=NULL WHERE id=?", (it["id"],))
        msg = f"{it['sku']} is no longer in a bin."
    else:
        db.execute("INSERT OR IGNORE INTO bins (id, label) VALUES (?, ?)", (number, ""))
        db.execute("UPDATE items SET bin_id=? WHERE id=?", (number, it["id"]))
        msg = f"📦 {it['sku']} → bin {number}."
    db.commit()
    await interaction.response.send_message(msg)


@app_commands.describe(sku="item SKU, e.g. S0007", platform="where it sold", price="sale price")
async def sold(interaction: discord.Interaction, sku: str, platform: Literal["ebay", "poshmark", "facebook"],
               price: float):
    it = _item_by_sku(sku)
    if not it:
        await interaction.response.send_message(f"No item {sku}.")
        return
    if it["status"] in ("sold", "shipped"):
        await interaction.response.send_message(f"{it['sku']} is already marked sold.")
        return
    lst = db.execute("SELECT * FROM listings WHERE item_id=? AND platform_id=?", (it["id"], platform)).fetchone()
    lid = lst["id"] if lst else db.execute(
        "INSERT INTO listings (item_id, platform_id, status) VALUES (?,?,'live')", (it["id"], platform)).lastrowid
    # Listings that were approved but never posted just get closed: nothing to take down.
    db.execute("UPDATE listings SET status='ended', ended_at=datetime('now') WHERE item_id=? AND id<>? "
               "AND status IN ('draft','pending_approval','approved')", (it["id"], lid))
    db.execute("UPDATE approvals SET status='expired' WHERE kind='post_listing' AND ref_id=? AND status='pending'",
               (it["id"],))
    db.execute("INSERT INTO sales (item_id, listing_id, sale_price) VALUES (?,?,?)", (it["id"], lid, price))
    db.commit()   # trigger has now marked it sold and queued delist tasks
    for t in db.execute("SELECT d.id, l.platform_id FROM delist_tasks d JOIN listings l ON l.id=d.listing_id "
                        "WHERE d.status IN ('pending','manual_required') AND l.item_id=?", (it["id"],)).fetchall():
        status = "pending" if t["platform_id"] in auto_delist() else "manual_required"
        db.execute("UPDATE delist_tasks SET status=? WHERE id=?", (status, t["id"]))
    db.commit()
    others = db.execute("SELECT l.platform_id FROM delist_tasks d JOIN listings l ON l.id=d.listing_id "
                        "JOIN sales s ON s.id=d.reason_sale WHERE s.item_id=? AND d.status!='done'",
                        (it["id"],)).fetchall()
    where = f" It was in **bin {it['bin_id']}**." if it["bin_id"] is not None else ""
    if others:
        AUTO_DELIST = auto_delist()
        auto = [NAMES.get(o["platform_id"], o["platform_id"]) for o in others if o["platform_id"] in AUTO_DELIST]
        manual = [NAMES.get(o["platform_id"], o["platform_id"]) for o in others if o["platform_id"] not in AUTO_DELIST]
        msg = f"💰 {it['sku']} sold on {NAMES[platform]} for ${price:.2f}.{where}"
        if manual:
            msg += (f"\n⚠️ Still listed on: **{', '.join(manual)}**. Delist alerts are in #alerts; "
                    f"tap Done on each once it's removed.")
        if auto:
            msg += (f"\n🤖 Removing from **{', '.join(auto)}** automatically. You'll get an alert in "
                    f"#alerts if that fails or hasn't happened in 10 minutes.")
    else:
        msg = f"💰 {it['sku']} sold on {NAMES[platform]} for ${price:.2f}.{where} No other listings to remove."
    await interaction.response.send_message(msg)
    await bot.post_delist_alerts()


def register(client, tree: app_commands.CommandTree, database) -> None:
    global db, bot
    db, bot = database, client
    tree.command(name="find", description="Which bin is it in? Describe the item")(find)
    tree.command(name="bin", description="What's in a bin")(bin_)
    tree.command(name="move", description="Put an item in a bin")(move)
    tree.command(name="sold", description="Record a sale; get alerts to remove it everywhere else")(sold)
