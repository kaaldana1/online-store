"""Step 2 Discord flow: /newitem → AI draft → comps → prices → approval → copy-paste packets.

    /newitem photo [photo2] [photo3] [notes] [cost] [bin]
        saves the item, AI drafts titles/description/tags, posts an intake card with
        "eBay sold prices" / "Poshmark sold" links and an "Enter prices" button
    Enter prices (form)
        typical sold price, optional range, weight, cost → per-platform prices → #approvals
    Approve
        posts a thread with one copy-paste packet per platform + "Mark posted" buttons
    /today   progress toward the daily listing goal
    /spend   AI spend this month
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tomllib
import urllib.parse
import uuid
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "listing"))
import ai  # noqa: E402
import pricer  # noqa: E402

CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())
PHOTOS = ROOT / "data" / "photos"
PLATFORM_NAMES = {"ebay": "eBay", "poshmark": "Poshmark", "facebook": "Facebook (local)"}
TITLE_KEY = {"ebay": "ebay_title", "poshmark": "poshmark_title", "facebook": "facebook_title"}
TAGS_KEY = {"ebay": "tags", "poshmark": "poshmark_style_tags", "facebook": "facebook_tags"}
# Draft fields kept in appraisals.keywords (JSON) for the packets, the finder and later delisting.
META_KEYS = ("search_query", "brand", "material", "style_era", "skill_category", "identification_confidence",
             "evidence", "checks_before_listing", "blockers", "sources", "ebay_item_specifics",
             "poshmark_brand", "poshmark_category", "poshmark_size", "poshmark_colors", "poshmark_style_tags",
             "facebook_tags", "lint", "notices", "web_searches")

db = None          # set by register()
bot = None


# ------------------------------------------------------------ helpers
def _today_sql() -> str:
    off = CFG["goal"].get("tz_offset_hours", -7)
    return f"date(posted_at, '{off:+d} hours') = date('now', '{off:+d} hours')"


def posted_today() -> int:
    return db.execute(f"SELECT COUNT(DISTINCT item_id) FROM listings WHERE posted_at IS NOT NULL AND {_today_sql()}"
                      ).fetchone()[0]


def comps_view(item_id: int, query: str) -> discord.ui.View:
    q = urllib.parse.quote_plus(query)
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="eBay sold prices", emoji="🔎",
                                 url=f"https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1"))
    v.add_item(discord.ui.Button(label="Poshmark sold", emoji="👗",
                                 url=f"https://poshmark.com/search?query={q}&availability=sold_out"))
    v.add_item(EnterPricesButton(item_id))
    return v


def stop_lines(meta: dict) -> list[str]:
    """Things that should stop a listing: the AI's blockers plus listing-check errors still open."""
    out = [f"🛑 {b}" for b in meta.get("blockers", [])]
    out += [f"🛑 {i['field']}: {i['message']}" for i in meta.get("lint", []) if i["level"] == "error"]
    return out


def load_meta(item_id: int) -> dict:
    a = db.execute("SELECT keywords FROM appraisals WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    try:
        return json.loads(a["keywords"]) if a and a["keywords"] else {}
    except ValueError:
        return {}


def _num(s: str | None, default: float | None = None) -> float | None:
    if s is None or not s.strip():
        return default
    return float(s.strip().replace("$", "").replace(",", ""))


def spot_prices() -> tuple[dict, str]:
    """Spot prices set with /spot, plus a warning when they're older than stale_after_days."""
    spot, note = {}, ""
    stale = CFG["pricing"]["melt"].get("stale_after_days", 7)
    for metal in ("silver", "gold"):
        r = db.execute("SELECT value FROM bot_state WHERE key=?", (f"spot_{metal}",)).fetchone()
        if not r:
            continue
        price, _, when = r["value"].partition("@")
        spot[metal] = float(price)
        old = db.execute("SELECT julianday('now') - julianday(?) > ?", (when, stale)).fetchone()[0]
        if old:
            note = f" (⚠️ spot price is from {when[:10]}, update it with /spot)"
    return spot, note


# ------------------------------------------------------------ /newitem
async def newitem(interaction: discord.Interaction, photo: discord.Attachment,
                  photo2: discord.Attachment | None = None, photo3: discord.Attachment | None = None,
                  notes: str = "", cost: float = 0.0, bin: int | None = None):
    await interaction.response.defer(thinking=True)
    atts = [a for a in (photo, photo2, photo3) if a is not None]
    if any(not (a.content_type or "").startswith("image/") for a in atts):
        await interaction.followup.send("Those need to be photos (JPG/PNG).")
        return

    if bin is not None:
        db.execute("INSERT OR IGNORE INTO bins (id, label) VALUES (?, ?)", (bin, f"Bin {bin}"))
    item_id = db.execute("INSERT INTO items (sku, name, cost_basis, bin_id, sourced_at) VALUES (?,?,?,?,date('now'))",
                         (uuid.uuid4().hex, notes[:80] or "new item", cost, bin)).lastrowid
    sku = f"S{item_id:04d}"
    db.execute("UPDATE items SET sku=? WHERE id=?", (sku, item_id))

    PHOTOS.mkdir(parents=True, exist_ok=True)
    blobs = []
    for i, a in enumerate(atts, 1):
        data = await a.read()
        blobs.append(data)
        ext = Path(a.filename).suffix.lower() or ".jpg"
        path = PHOTOS / f"{sku}-{i}{ext}"
        path.write_bytes(data)
        db.execute("INSERT INTO item_photos (item_id, path, is_primary) VALUES (?,?,?)",
                   (item_id, str(path.relative_to(ROOT)), int(i == 1)))
    db.commit()

    try:
        d = await asyncio.to_thread(ai.identify, blobs, notes, sku)
    except Exception as e:  # keep the item; she can still price it by hand
        d = None
        err = str(e)

    if d:
        db.execute("UPDATE items SET name=?, description=?, category=?, condition=?, weight_oz=?, status='appraised' "
                   "WHERE id=?", (d["name"], d["description"], d["category"],
                                  f"{d['condition']}. {d['condition_notes']}".strip(),
                                  round(d["est_weight_lb"] * 16, 1), item_id))
        meta = {k: d.get(k) for k in META_KEYS}
        db.execute("INSERT INTO appraisals (item_id, keywords, method) VALUES (?,?, 'pending_comps')",
                   (item_id, json.dumps(meta)))
        for pid in PLATFORM_NAMES:
            db.execute("INSERT INTO listings (item_id, platform_id, title, description, tags) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(item_id, platform_id) DO UPDATE SET title=excluded.title, "
                       "description=excluded.description, tags=excluded.tags",
                       (item_id, pid, d[TITLE_KEY[pid]], d["description"], json.dumps(d[TAGS_KEY[pid]])))
        db.commit()
        query = d["search_query"]
        stops = stop_lines(meta)
        e = discord.Embed(title=f"{sku} · {d['name']}"[:256],
                          color=discord.Color.red() if stops else discord.Color.blurple(),
                          description=f"**eBay title:** {d['ebay_title']}")
        if stops:
            e.add_field(name="🛑 Don't list until this is sorted out", value="\n".join(stops)[:1024], inline=False)
        e.add_field(name="Category", value=d["category"])
        e.add_field(name="Condition", value=d["condition"])
        e.add_field(name="Est. weight", value=f"{d['est_weight_lb']:g} lb")
        e.add_field(name="Brand / material", value=f"{d['brand']} · {d['material']}"[:1024], inline=False)
        sure = {"high": "high", "medium": "medium (some details inferred)", "low": "low (best guess)"}
        ident = f"Confidence: {sure.get(d['identification_confidence'], d['identification_confidence'] or '-')}"
        if d["evidence"]:
            ident += "\n" + "\n".join(f"• {x}" for x in d["evidence"][:4])
        if d["sources"]:
            ident += "\n" + "\n".join(f"🔗 <{s['url']}> {s['confirmed']}" for s in d["sources"][:3])
        e.add_field(name="What it's based on", value=ident[:1024], inline=False)
        if d["checks_before_listing"]:
            e.add_field(name="⚠️ Check before listing",
                        value="\n".join(f"• {c}" for c in d["checks_before_listing"])[:1024], inline=False)
        fixes = [f"✏️ {i['field']}: {i['message']}" for i in d["lint"] if i["level"] == "warn"]
        if fixes:
            e.add_field(name="✏️ Worth a look", value="\n".join(fixes)[:1024], inline=False)
        if d["notices"]:
            e.add_field(name="ℹ️ Note", value="\n".join(d["notices"])[:1024], inline=False)
        e.add_field(name="Next", value="1. Tap **eBay sold prices** and look at what similar items actually sold for.\n"
                                       "2. Tap **Enter prices** and type the typical sold price.", inline=False)
    else:
        for pid in PLATFORM_NAMES:
            db.execute("INSERT OR IGNORE INTO listings (item_id, platform_id, title) VALUES (?,?,?)",
                       (item_id, pid, notes[:80]))
        db.commit()
        query = notes or "item"
        e = discord.Embed(title=f"{sku} · saved without an AI draft", color=discord.Color.orange(),
                          description=f"{err}\nYou can still look up comps and enter prices; titles will use your notes.")
    if bin is not None:
        e.set_footer(text=f"Stored in bin {bin}")
    e.set_thumbnail(url=photo.url)
    await interaction.followup.send(embed=e, view=comps_view(item_id, query))


# ------------------------------------------------------------ prices form
class PriceModal(discord.ui.Modal):
    def __init__(self, item_id: int):
        item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        super().__init__(title=f"Prices for {item['sku']}"[:45])
        self.item_id = item_id
        self.typical = discord.ui.TextInput(label="Typical SOLD price ($)", placeholder="e.g. 40", max_length=10)
        self.range = discord.ui.TextInput(label="Sold range, optional (low-high)", placeholder="e.g. 30-55",
                                          required=False, max_length=20)
        self.weight = discord.ui.TextInput(label="Packed weight (lb)", max_length=10,
                                           default=f"{(item['weight_oz'] or 16) / 16:g}")
        self.cost = discord.ui.TextInput(label="What you paid ($)", max_length=10,
                                         default=f"{item['cost_basis'] or 0:g}")
        self.metal = discord.ui.TextInput(label="Sterling/gold weight + purity, optional",
                                          placeholder="e.g. 31g 925 or 4.2g 14k (sets a melt-value floor)",
                                          required=False, max_length=30, default=load_meta(item_id).get("metal_text"))
        for t in (self.typical, self.range, self.weight, self.cost, self.metal):
            self.add_item(t)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            market = _num(self.typical.value)
            weight = _num(self.weight.value, 1.0)
            cost = _num(self.cost.value, 0.0)
            lo = hi = None
            if self.range.value.strip():
                lo, hi = (float(x) for x in re.findall(r"[\d.]+", self.range.value)[:2])
        except (ValueError, TypeError):
            await interaction.response.send_message("Couldn't read those numbers. Use plain numbers like 40 or 30-55.",
                                                    ephemeral=True)
            return
        try:
            metal = pricer.parse_metal(self.metal.value)
        except ValueError as e:
            await interaction.response.send_message(f"Couldn't read the metal weight ({e}). Try `31g 925` or `4.2g 14k`, "
                                                    f"or leave it blank.", ephemeral=True)
            return
        item = db.execute("SELECT * FROM items WHERE id=?", (self.item_id,)).fetchone()
        db.execute("UPDATE items SET cost_basis=?, weight_oz=?, status='drafted' WHERE id=?",
                   (cost, weight * 16, self.item_id))
        db.execute("UPDATE appraisals SET price_low=?, price_median=?, price_high=?, method='manual_comps' "
                   "WHERE item_id=? AND id=(SELECT MAX(id) FROM appraisals WHERE item_id=?)",
                   (lo, market, hi, self.item_id, self.item_id))
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            db.execute("INSERT INTO appraisals (item_id, price_low, price_median, price_high, method) "
                       "VALUES (?,?,?,?, 'manual_comps')", (self.item_id, lo, market, hi))

        spot, spot_note = spot_prices()
        meta = load_meta(self.item_id)
        meta["metal_text"] = (self.metal.value or "").strip() or None
        db.execute("UPDATE appraisals SET keywords=? WHERE item_id=? AND id=(SELECT MAX(id) FROM appraisals WHERE item_id=?)",
                   (json.dumps(meta), self.item_id, self.item_id))
        plans = pricer.plan(market, low=lo, high=hi, category=item["category"] or "default", cost=cost,
                            weight_lb=weight, metal=metal, spot=spot)
        lines, payload = [], []
        melt = pricer.melt_floor(metal, spot)
        if metal and melt is None:
            lines.append(f"⚠️ No {metal.metal} spot price set, so there's no melt floor. Set one with `/spot`.")
        elif melt is not None:
            lines.append(f"🪙 Melt floor ${melt:.0f} ({metal.label()}){spot_note}")
            if pricer.under_melt(market, metal, spot):
                lines.append("⚠️ Typical comps sell **below melt value**: consider selling it as a lot or to a refiner.")
        for p in plans:
            name = PLATFORM_NAMES.get(p.platform, p.platform)
            if p.skip_reason:
                lines.append(f"• ~~{name}~~: skipped, {p.skip_reason} at ${p.list_price:.0f}")
                db.execute("UPDATE listings SET status='draft', list_price=?, est_net=? WHERE item_id=? AND platform_id=?",
                           (p.list_price, p.est_net, self.item_id, p.platform))
                continue
            lines.append(f"• **{name}**: list **${p.list_price:.0f}** · accept offers ≥ ${p.floor_price:.0f} · "
                         f"expected profit ≈ ${p.expected_net:.2f} (${p.est_net:.2f} at full price)"
                         + (f" · {'; '.join(p.notes)}" if p.notes else ""))
            payload.append(p.__dict__)
            db.execute("UPDATE listings SET status='pending_approval', list_price=?, floor_price=?, est_net=? "
                       "WHERE item_id=? AND platform_id=?",
                       (p.list_price, p.floor_price, p.est_net, self.item_id, p.platform))
        if not payload:
            db.commit()
            await interaction.response.send_message(
                f"{item['sku']}: no platform clears the ${CFG['pricing']['min_profit']:.0f} minimum profit at "
                f"${market:.0f}.\n" + "\n".join(lines))
            return

        db.execute("UPDATE approvals SET status='expired' WHERE kind='post_listing' AND ref_id=? AND status='pending'",
                   (self.item_id,))
        rng = f" (range ${lo:.0f}–${hi:.0f})" if lo is not None and hi is not None else ""
        stops = stop_lines(load_meta(self.item_id))
        warn = ("\n\n**Open problems from the draft:**\n" + "\n".join(stops)) if stops else ""
        summary = (f"**{item['sku']} · {item['name']}**  ({item['category']})\n"
                   f"Typical sold price: ${market:.0f}{rng} · paid ${cost:g} · {weight:g} lb\n\n" + "\n".join(lines) +
                   warn + "\n\nApprove → I'll post copy-paste listings for each platform.")[:4000]
        db.execute("INSERT INTO approvals (kind, ref_table, ref_id, summary, payload) VALUES "
                   "('post_listing','items',?,?,?)", (self.item_id, summary, json.dumps(payload)))
        db.commit()
        await interaction.response.send_message(f"✅ {item['sku']} priced and sent to approvals.\n" + "\n".join(lines))
        await bot.post_new_approvals()


class EnterPricesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"prices:(?P<id>\d+)"):
    def __init__(self, item_id: int):
        self.item_id = item_id
        super().__init__(discord.ui.Button(label="Enter prices", emoji="💲", style=discord.ButtonStyle.primary,
                                           custom_id=f"prices:{item_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PriceModal(self.item_id))


# ------------------------------------------------------------ approval → packets
def packet_text(listing, item, meta: dict) -> list[str]:
    pid = listing["platform_id"]
    tags = json.loads(listing["tags"] or "[]")
    head = (f"## {PLATFORM_NAMES.get(pid, pid)} · {item['sku']}\n"
            f"Price: **${listing['list_price']:.0f}** · lowest offer to accept: **${listing['floor_price']:.0f}** · "
            f"profit ≈ ${listing['est_net']:.2f}\n**Title**\n```\n{listing['title']}\n```\n")
    if pid == "ebay":
        head += f"**Condition:** {item['condition'] or '-'}"
        specs = meta.get("ebay_item_specifics") or []
        if specs:
            head += "\n**Item specifics**\n" + "\n".join(f"• {s['name']}: {s['value']}" for s in specs[:20])
    elif pid == "poshmark":
        head += (f"**Brand:** {meta.get('poshmark_brand') or '-'} · **Category:** {meta.get('poshmark_category') or '-'}"
                 f" · **Size:** {meta.get('poshmark_size') or '-'}\n"
                 f"**Colors:** {', '.join(meta.get('poshmark_colors') or []) or '-'}\n"
                 f"**Style tags:** {', '.join(tags[:3]) or '-'}")
    else:
        area = CFG.get("facebook", {}).get("pickup_area", "").strip()
        head += f"**Tags:** {', '.join(tags) or '-'}\nLocal pickup only" + (f": {area}." if area else ".")
    for s in stop_lines(meta):
        head += "\n" + s
    checks = meta.get("checks_before_listing") or []
    if checks:
        head += "\n⚠️ " + " · ".join(checks)
    desc = f"**Description**\n```\n{(listing['description'] or '').strip()[:1850]}\n```"
    return [head + "\n" + desc] if len(head) + len(desc) < 1990 else [head[:1990], desc]


async def on_listing_approved(interaction: discord.Interaction, approval) -> None:
    item_id = approval["ref_id"]
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    meta = load_meta(item_id)
    db.execute("UPDATE listings SET status='approved' WHERE item_id=? AND status='pending_approval'", (item_id,))
    db.commit()
    rows = db.execute("SELECT * FROM listings WHERE item_id=? AND status='approved' ORDER BY est_net DESC",
                      (item_id,)).fetchall()
    thread = await interaction.message.create_thread(name=f"{item['sku']} listings · copy & paste",
                                                     auto_archive_duration=1440)
    await thread.send("Long-press a grey box to copy it. Post on each site, then tap **Mark posted**.")
    for r in rows:
        parts = packet_text(r, item, meta)
        for part in parts[:-1]:
            await thread.send(part)
        v = discord.ui.View(timeout=None)
        v.add_item(MarkPostedButton(r["id"]))
        await thread.send(parts[-1], view=v)
    db.execute("UPDATE approvals SET status='applied' WHERE id=?", (approval["id"],))
    db.commit()


async def on_listing_rejected(interaction: discord.Interaction, approval) -> None:
    db.execute("UPDATE listings SET status='draft' WHERE item_id=? AND status='pending_approval'", (approval["ref_id"],))
    db.commit()


class MarkPostedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"posted:(?P<id>\d+)"):
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(label="Mark posted", emoji="📤", style=discord.ButtonStyle.success,
                                           custom_id=f"posted:{listing_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        r = db.execute("SELECT l.*, i.sku FROM listings l JOIN items i ON i.id=l.item_id WHERE l.id=?",
                       (self.listing_id,)).fetchone()
        if r["status"] == "live":
            await interaction.response.send_message("Already marked posted.", ephemeral=True)
            return
        # Ask for the listing's link: delist alerts and sale-email matching use it.
        await interaction.response.send_modal(PostedLinkModal(self.listing_id, r["sku"], r["platform_id"]))


class PostedLinkModal(discord.ui.Modal):
    def __init__(self, listing_id: int, sku: str, platform_id: str):
        super().__init__(title=f"Posted {sku} on {PLATFORM_NAMES.get(platform_id, platform_id)}"[:45])
        self.listing_id = listing_id
        self.link = discord.ui.TextInput(
            label="Listing link (paste it from the site)", required=False, max_length=500,
            placeholder="https://... (puts the link in delist alerts)")
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        r = db.execute("SELECT l.*, i.sku FROM listings l JOIN items i ON i.id=l.item_id WHERE l.id=?",
                       (self.listing_id,)).fetchone()
        url = (self.link.value or "").strip() or None
        db.execute("UPDATE listings SET status='live', posted_at=datetime('now'), url=COALESCE(?, url) WHERE id=?",
                   (url, self.listing_id))
        db.execute("UPDATE items SET status='listed' WHERE id=?", (r["item_id"],))
        db.commit()
        n, goal = posted_today(), CFG["goal"]["listings_per_day"]
        await interaction.response.edit_message(view=None)
        note = "" if url else " (no link saved, so a delist alert for it won't have a link)"
        await interaction.followup.send(
            f"📤 {r['sku']} posted on {PLATFORM_NAMES.get(r['platform_id'], r['platform_id'])} by "
            f"{interaction.user.display_name}{note}. Today: **{n}/{goal}** items listed" + (" 🎉" if n >= goal else "."))


# ------------------------------------------------------------ /today, /spend
async def today(interaction: discord.Interaction):
    n, goal = posted_today(), CFG["goal"]["listings_per_day"]
    waiting = db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    unpriced = db.execute("SELECT COUNT(*) FROM items WHERE status IN ('sourced','appraised')").fetchone()[0]
    bar = "🟩" * min(n, goal) + "⬜" * max(goal - n, 0)
    msg = f"**Today:** {bar} {n}/{goal} items listed\nWaiting for approval: {waiting} · drafted but not priced: {unpriced}"
    # Approved packets nobody marked posted: either post them, or tap Mark posted if they're already up
    # (otherwise a sale elsewhere can't be matched and the alert has no link).
    unposted = db.execute("SELECT i.sku, GROUP_CONCAT(l.platform_id, ', ') AS where_ FROM listings l "
                          "JOIN items i ON i.id=l.item_id WHERE l.status='approved' AND i.status NOT IN ('sold','shipped') "
                          "GROUP BY i.id ORDER BY i.id").fetchall()
    if unposted:
        msg += ("\n📤 Approved but not marked posted: " +
                ", ".join(f"`{r['sku']}` ({r['where_']})" for r in unposted[:15]) +
                ("…" if len(unposted) > 15 else "") + "\nPost them, or tap **Mark posted** in their thread if they're up.")
    ship = db.execute("SELECT COUNT(*) FROM items WHERE status='sold'").fetchone()[0]
    if ship:
        msg += f"\n📦 {ship} sold item{'' if ship == 1 else 's'} to ship: `/toship`"
    if hasattr(bot, "inbox_status"):
        msg += "\n" + bot.inbox_status()
    await interaction.response.send_message(msg[:2000])


@app_commands.describe(metal="silver or gold", usd_per_troy_oz="today's spot price per troy ounce")
async def spot(interaction: discord.Interaction, metal: Literal["silver", "gold"], usd_per_troy_oz: float):
    db.execute("INSERT INTO bot_state (key, value) VALUES (?, ? || '@' || datetime('now')) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"spot_{metal}", str(usd_per_troy_oz)))
    db.commit()
    payout = CFG["pricing"]["melt"]["dealer_payout"]
    per_g = usd_per_troy_oz / pricer.TROY_OZ_G * (0.925 if metal == "silver" else 0.585) * payout
    await interaction.response.send_message(
        f"🪙 {metal.title()} spot set to ${usd_per_troy_oz:.2f}/ozt. Melt floor ≈ ${per_g:.2f} per gram of "
        f"{'sterling' if metal == 'silver' else '14k'} (at {payout:.0%} dealer payout).")


async def spend(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM spend_this_month").fetchall()
    total = sum(r["usd"] or 0 for r in rows)
    body = "\n".join(f"{r['agent']:<10} {r['runs']:>4} runs  ${r['usd'] or 0:.2f}" for r in rows) or "no AI calls yet"
    await interaction.response.send_message(f"**AI spend this month: ${total:.2f}**\n```\n{body}\n```")


# ------------------------------------------------------------ wiring
def register(client, tree: app_commands.CommandTree, database, hooks: dict) -> None:
    global db, bot
    db, bot = database, client
    client.add_dynamic_items(EnterPricesButton, MarkPostedButton)
    tree.command(name="newitem", description="Add a thrifted item: photos in, listing drafts out")(
        app_commands.describe(photo="main photo", photo2="extra photo (hallmark, label, flaw)", photo3="extra photo",
                              notes="anything you know: brand, '925 stamp', flaws", cost="what you paid",
                              bin="storage bin number")(newitem))
    tree.command(name="today", description="Progress toward today's listing goal")(today)
    tree.command(name="spend", description="AI spend this month")(spend)
    tree.command(name="spot", description="Set today's silver or gold spot price (for melt-value floors)")(spot)
    hooks[("post_listing", "approved")] = on_listing_approved
    hooks[("post_listing", "rejected")] = on_listing_rejected
