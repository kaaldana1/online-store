"""Step 3 Discord commands: /find, /bin, /move, /sold, /unsold.

/sold is the cross-platform safety net: recording a sale marks it sold and the database
creates a delist task for every OTHER listing of that item. Those show up in #alerts
(with re-pings) until someone confirms each one was taken down. Sales on eBay, Poshmark,
Mercari and Depop are also picked up from their emails (bot/sales_watch.py); Facebook
cash sales need /sold. /unsold undoes a sale (cancellation, return, no-show).
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
sys.path.insert(0, str(ROOT / "sales"))
import sale_record as sr  # noqa: E402
import search  # noqa: E402

STORAGE_URL = os.environ.get("STORAGE_URL", "http://localhost:8765")
db = None
bot = None


def _item_by_sku(sku: str):
    return sr.item_by_sku(db, sku)


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
async def sold(interaction: discord.Interaction, sku: str,
               platform: Literal["ebay", "poshmark", "facebook", "mercari", "depop"], price: float):
    it = _item_by_sku(sku)
    if not it:
        await interaction.response.send_message(f"No item {sku}.")
        return
    try:
        res = sr.record_sale(db, it["id"], platform, price, source="discord")
    except sr.AlreadySold as e:
        await interaction.response.send_message(f"{e} If that was wrong, `/unsold {it['sku']}` first.")
        return
    where = f" It was in **bin {it['bin_id']}**." if it["bin_id"] is not None else ""
    msg = f"💰 {it['sku']} sold on {sr.name(platform)} for ${price:.2f}.{where}"
    if res.to_remove:
        msg += (f"\n⚠️ Still listed on: **{', '.join(sr.name(p) for p in res.to_remove)}**. Delist alerts are in "
                f"#alerts; tap Done on each once it's removed.")
    else:
        msg += " No other listings to remove."
    await interaction.response.send_message(msg)
    await bot.post_delist_alerts()


@app_commands.describe(sku="item SKU, e.g. S0007", why="optional: cancelled, returned, no-show...")
async def unsold(interaction: discord.Interaction, sku: str, why: str = ""):
    it = _item_by_sku(sku)
    if not it:
        await interaction.response.send_message(f"No item {sku}.")
        return
    try:
        res = sr.void_sale(db, it["id"], why)
    except ValueError as e:
        await interaction.response.send_message(str(e))
        return
    msg = f"↩️ {it['sku']} is back on sale (the {sr.name(res.platform)} sale was undone{': ' + why if why else ''})."
    msg += f"\n• Make sure the **{sr.name(res.platform)}** listing is active again (relist it if the site closed it)."
    if res.still_up:
        msg += f"\n• Still up on **{', '.join(sr.name(p) for p in res.still_up)}**: those delist alerts are cancelled."
    if res.removed:
        msg += (f"\n• Already removed from **{', '.join(sr.name(p) for p in res.removed)}**: relist there if you want "
                f"(the copy-paste packet is in the item's listings thread).")
    await interaction.response.send_message(msg)


def register(client, tree: app_commands.CommandTree, database) -> None:
    global db, bot
    db, bot = database, client
    tree.command(name="find", description="Which bin is it in? Describe the item")(find)
    tree.command(name="bin", description="What's in a bin")(bin_)
    tree.command(name="move", description="Put an item in a bin")(move)
    tree.command(name="sold", description="Record a sale; get alerts to remove it everywhere else")(sold)
    tree.command(name="unsold", description="Undo a sale: cancelled, returned, or a no-show")(unsold)
