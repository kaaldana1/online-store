"""Store team Discord bot: the skeleton for step 1.

What it does now:
  * /fees     shows the price-vs-profit comparison across platforms (pricing/fees.py)
  * /pending  re-posts every approval that is still waiting
  * Posts each new row in `approvals` to #approvals with Approve / Reject buttons.
    The buttons survive bot restarts.
  * Posts every delist task to #alerts with the listing link, the exact steps, and a
    "Done, I delisted it" button, and re-pings every 2 hours until someone presses it.
    Every delist is done by a person; nothing logs in to a marketplace.
  * Watches the sales inbox for marketplace sale emails (bot/sales_watch.py).

Later steps plug in by inserting rows into `approvals` / `delist_tasks`. The bot never
posts, prices, or replies on a platform by itself.

Env vars (put them in bot/.env, see .env.example):
  DISCORD_TOKEN, GUILD_ID, APPROVALS_CHANNEL_ID, ALERTS_CHANNEL_ID, ANTHROPIC_API_KEY
Step 2 commands (/newitem, /today, /spend) live in bot/listing_ui.py.
Step 3: /find, /bin, /move, /sold, /unsold in bot/storage_cmds.py; storage map web page in bot/web.py.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pricing"))
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "sales"))
import fees as feecalc  # noqa: E402
import sale_record as sr  # noqa: E402
from init_db import connect, init  # noqa: E402
import listing_ui  # noqa: E402  (step 2: /newitem, prices, packets)
import sales_watch  # noqa: E402  (sale emails → sales, with alerts)
import storage_cmds  # noqa: E402  (step 3: /find, /bin, /move, /sold, /unsold)
import web  # noqa: E402  (step 3: storage map at http://localhost:8765)


def _load_env() -> None:
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
GUILD = discord.Object(id=int(os.environ["GUILD_ID"])) if os.environ.get("GUILD_ID") else None
APPROVALS_CH = int(os.environ.get("APPROVALS_CHANNEL_ID", 0))
ALERTS_CH = int(os.environ.get("ALERTS_CHANNEL_ID", 0))
REPING_EVERY = timedelta(hours=2)

db = connect()
init(db)

# (approval kind, decision) -> async fn(interaction, approval_row). Filled by feature modules.
HOOKS: dict = {}


# ------------------------------------------------------------ persistent buttons
class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"approval:(?P<action>approve|reject):(?P<id>\d+)"):
    def __init__(self, action: str, approval_id: int):
        self.action, self.approval_id = action, approval_id
        super().__init__(discord.ui.Button(
            label="Approve" if action == "approve" else "Reject",
            style=discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger,
            custom_id=f"approval:{action}:{approval_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        row = db.execute("SELECT * FROM approvals WHERE id=?", (self.approval_id,)).fetchone()
        if row is None or row["status"] != "pending":
            await interaction.response.send_message(
                f"Already {row['status'] if row else 'gone'}.", ephemeral=True)
            return
        status = "approved" if self.action == "approve" else "rejected"
        db.execute("UPDATE approvals SET status=?, decided_at=datetime('now'), decided_by=? WHERE id=?",
                   (status, str(interaction.user), self.approval_id))
        db.commit()
        # Workers (step 2+) pick up 'approved' rows and apply them, then set 'applied' or 'failed'.
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green() if status == "approved" else discord.Color.red()
        embed.set_footer(text=f"{status} by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)
        hook = HOOKS.get((row["kind"], status))
        if hook:
            try:
                await hook(interaction, row)
            except Exception as e:  # never leave the seller wondering
                db.execute("UPDATE approvals SET status='failed' WHERE id=?", (self.approval_id,))
                db.commit()
                await interaction.followup.send(f"⚠️ Approved, but the follow-up step failed: {e}")


class DelistDoneButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"delist:done:(?P<id>\d+)"):
    def __init__(self, task_id: int):
        self.task_id = task_id
        super().__init__(discord.ui.Button(label="Done, I delisted it", style=discord.ButtonStyle.primary,
                                           custom_id=f"delist:done:{task_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        db.execute("UPDATE delist_tasks SET status='done', done_at=datetime('now') WHERE id=?", (self.task_id,))
        db.execute("UPDATE listings SET status='ended', ended_at=datetime('now') "
                   "WHERE id=(SELECT listing_id FROM delist_tasks WHERE id=?) AND status IN ('live','approved','delist_failed')",
                   (self.task_id,))
        db.commit()
        await interaction.response.edit_message(content=f"✅ Delisted (confirmed by {interaction.user.display_name})",
                                                embed=None, view=None)


def approval_view(aid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ApprovalButton("approve", aid))
    v.add_item(ApprovalButton("reject", aid))
    return v


def approval_embed(row) -> discord.Embed:
    e = discord.Embed(title=f"Needs approval: {row['kind'].replace('_', ' ')}",
                      description=row["summary"], color=discord.Color.gold())
    e.set_footer(text=f"approval #{row['id']}")
    return e


# ------------------------------------------------------------ client
class StoreBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.last_ping: dict[int, datetime] = {}

    async def setup_hook(self):
        self.add_dynamic_items(ApprovalButton, DelistDoneButton)
        if GUILD:
            self.tree.copy_global_to(guild=GUILD)
            await self.tree.sync(guild=GUILD)
        else:
            await self.tree.sync()
        self.watch.start()
        sales_watch.start()
        try:
            self.web = await web.start(db, port=int(os.environ.get("WEB_PORT", 8765)))
        except OSError as e:
            print(f"Storage map NOT started: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} | approvals channel: "
              f"{'OK' if self.get_channel(APPROVALS_CH) else 'NOT FOUND'} | alerts channel: "
              f"{'OK' if self.get_channel(ALERTS_CH) else 'NOT FOUND'}")

    @tasks.loop(seconds=30)
    async def watch(self):
        await self.post_new_approvals()
        await self.post_delist_alerts()

    @watch.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    async def post_new_approvals(self):
        ch = self.get_channel(APPROVALS_CH)
        if ch is None:
            return
        for row in db.execute("SELECT * FROM approvals WHERE status='pending' AND discord_message_id IS NULL"):
            msg = await ch.send(embed=approval_embed(row), view=approval_view(row["id"]))
            db.execute("UPDATE approvals SET discord_message_id=? WHERE id=?", (str(msg.id), row["id"]))
        db.commit()

    async def post_delist_alerts(self):
        ch = self.get_channel(ALERTS_CH)
        if ch is None:
            return
        now = datetime.now(timezone.utc)
        rows = db.execute("""
            SELECT d.id, d.status, d.last_error, l.platform_id, l.url, l.status AS lstatus, l.title,
                   i.sku, i.name, i.bin_id, p.name AS pname
            FROM delist_tasks d JOIN listings l ON l.id=d.listing_id
            JOIN items i ON i.id=l.item_id JOIN platforms p ON p.id=l.platform_id
            WHERE d.status IN ('failed','manual_required')
               OR (d.status = 'pending' AND d.created_at < datetime('now', '-10 minutes'))""").fetchall()
        for r in rows:
            last = self.last_ping.get(r["id"])
            if last and now - last < REPING_EVERY:
                continue
            why = {"manual_required": "is still listed there. Take it down now",
                   "pending": "is waiting on an automatic step that hasn't happened after 10 minutes. "
                              "Take it down by hand"}.get(
                       r["status"], f"couldn't be removed automatically ({r['last_error'] or 'unknown error'}). "
                                    f"Take it down by hand")
            if r["lstatus"] == "approved":
                why = why.replace("is still listed there", "may be listed there")
            lines = [f"**{r['name'] or r['sku']}** {why}."]
            if r["lstatus"] == "approved":
                lines.append("It was approved but never marked posted. If you didn't post it there, just tap Done.")
            lines.append(f"🔗 {r['url']}" if r["url"] else
                         f"No link saved: search your {r['pname']} listings for **{r['sku']}** "
                         f"or “{(r['title'] or r['name'] or '')[:60]}”.")
            if r["platform_id"] in sr.DELIST_STEPS:
                lines.append(f"**How:** {sr.DELIST_STEPS[r['platform_id']]}")
            e = discord.Embed(title=f"⚠️ SOLD: remove {r['sku']} from {r['pname']}",
                              description="\n".join(lines), color=discord.Color.red())
            v = discord.ui.View(timeout=None)
            v.add_item(DelistDoneButton(r["id"]))
            await ch.send(content="@here" if last else None, embed=e, view=v)
            self.last_ping[r["id"]] = now


bot = StoreBot()
listing_ui.register(bot, bot.tree, db, HOOKS)
storage_cmds.register(bot, bot.tree, db)
sales_watch.register(bot, db, ALERTS_CH)


@bot.tree.command(description="Compare fees and profit across platforms for a price")
@app_commands.describe(price="list price", category="fee category, e.g. jewelry",
                       label="shipping label you pay", cost="what you paid for it",
                       weight="packed weight in lb (Poshmark charges extra over 5 lb)")
async def fees(interaction: discord.Interaction, price: float, category: str = "default",
               label: float = 0.0, cost: float = 0.0, weight: float = 0.0):
    table = feecalc.load_fees()
    rows = feecalc.compare(price, category=category, label_cost=label, cost_basis=cost,
                           weight_lb=weight, fees=table)
    lines = [f"{table[b.platform]['name']:<21} fees ${b.fees:>6.2f}   net ${b.net_profit:>7.2f}"
             + (f"  ({'; '.join(b.notes)})" if b.notes else "") for b in rows]
    head = f"**${price:.2f} · {category}" + (f" · {weight:g} lb" if weight else "") + "**"
    await interaction.response.send_message(head + "\n```\n" + "\n".join(lines) + "\n```")


@bot.tree.command(description="Re-post everything still waiting for approval")
async def pending(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        await interaction.response.send_message("Nothing waiting. 🎉")
        return
    await interaction.response.send_message(f"{len(rows)} waiting:")
    for row in rows:
        await interaction.followup.send(embed=approval_embed(row), view=approval_view(row["id"]))


@bot.tree.command(description="Is the sales inbox being checked? Checks it right now")
async def inbox(interaction: discord.Interaction):
    if sales_watch.ew.ImapConfig.from_env() is None:
        await interaction.response.send_message(sales_watch.status_line() +
                                                "\nSet EMAIL_ADDRESS and EMAIL_APP_PASSWORD in bot/.env to turn it on.")
        return
    await interaction.response.defer(thinking=True)
    n = await sales_watch.poll_once()
    head = "Checked just now: " + (f"{n} new marketplace email{'' if n == 1 else 's'}." if n >= 0 else "FAILED.")
    await interaction.followup.send(f"{head}\n{sales_watch.status_line()}")


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
