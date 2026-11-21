"""Record (and undo) a sale. Shared by /sold, /unsold and the sale email watcher.

Inserting the `sales` row is what matters: the database trigger then marks the item sold,
closes listings that were never posted, and creates a manual delist task for every other
listing that may be up. bot.py turns those tasks into #alerts with re-pings until Done.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

NAMES = {"ebay": "eBay", "poshmark": "Poshmark", "facebook": "Facebook", "mercari": "Mercari", "depop": "Depop"}

# Shown in every delist alert so removing a listing is a 10-second job.
DELIST_STEPS = {
    "poshmark": "Open the listing → **Edit Listing** → **Availability: Not For Sale** → **Update**.",
    "ebay": "My eBay → **Selling** → **Active** → the item → **More actions** → **End listing**.",
    "facebook": "Marketplace → **Your listings** → the item → **Mark as sold** (or **Delete**).",
    "mercari": "Open the listing → **Edit** → **Deactivate** (or **Delete**).",
    "depop": "Open the listing → **Edit** → **Delete** (or mark it sold).",
}


def name(platform: str) -> str:
    return NAMES.get(platform, platform)


class AlreadySold(Exception):
    def __init__(self, item, sale):
        self.item, self.sale = item, sale
        where = name(sale["platform_id"]) if sale else "somewhere"
        super().__init__(f"{item['sku']} is already recorded as sold on {where}.")


@dataclass
class SaleResult:
    sale_id: int
    item: sqlite3.Row
    platform: str
    price: float
    to_remove: list[str] = field(default_factory=list)      # platforms that now have a delist alert


def item_by_sku(db: sqlite3.Connection, sku: str):
    return db.execute("SELECT * FROM items WHERE upper(sku)=upper(?)", (sku.strip(),)).fetchone()


def active_sale(db: sqlite3.Connection, item_id: int):
    return db.execute("SELECT s.*, l.platform_id FROM sales s JOIN listings l ON l.id=s.listing_id "
                      "WHERE s.item_id=? AND s.voided_at IS NULL ORDER BY s.id DESC LIMIT 1", (item_id,)).fetchone()


def record_sale(db: sqlite3.Connection, item_id: int, platform: str, price: float,
                source: str = "discord") -> SaleResult:
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise ValueError(f"No item with id {item_id}.")
    prior = active_sale(db, item_id)
    if prior is not None or item["status"] in ("sold", "shipped"):
        raise AlreadySold(item, prior)
    lst = db.execute("SELECT * FROM listings WHERE item_id=? AND platform_id=?", (item_id, platform)).fetchone()
    lid = lst["id"] if lst else db.execute(
        "INSERT INTO listings (item_id, platform_id, status, posted_at) VALUES (?,?,'live',datetime('now'))",
        (item_id, platform)).lastrowid
    sale_id = db.execute("INSERT INTO sales (item_id, listing_id, sale_price, source) VALUES (?,?,?,?)",
                         (item_id, lid, price, source)).lastrowid
    db.commit()   # the trigger has now marked it sold and queued the delist tasks
    others = db.execute("SELECT l.platform_id FROM delist_tasks d JOIN listings l ON l.id=d.listing_id "
                        "WHERE d.reason_sale=? AND d.status!='done' ORDER BY l.platform_id", (sale_id,)).fetchall()
    return SaleResult(sale_id, item, platform, price, [o["platform_id"] for o in others])


@dataclass
class UndoResult:
    item: sqlite3.Row
    platform: str
    still_up: list[str]          # delist not done yet, so the listing is still up: left live
    removed: list[str]           # already taken down: she can relist there if she wants


def void_sale(db: sqlite3.Connection, item_id: int, why: str = "") -> UndoResult:
    """Put an item back on sale (buyer cancelled, return, Facebook no-show)."""
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    sale = active_sale(db, item_id)
    if sale is None:
        raise ValueError(f"{item['sku']} has no recorded sale to undo.")
    db.execute("UPDATE sales SET voided_at=datetime('now') WHERE id=?", (sale["id"],))
    db.execute("UPDATE listings SET status='live', ended_at=NULL WHERE id=?", (sale["listing_id"],))
    still_up, removed = [], []
    for t in db.execute("SELECT d.id, d.status, l.platform_id FROM delist_tasks d "
                        "JOIN listings l ON l.id=d.listing_id WHERE d.reason_sale=?", (sale["id"],)).fetchall():
        if t["status"] == "done":
            removed.append(t["platform_id"])
        else:
            # Not removed yet, so it's still up: cancel the alert and keep the listing live.
            db.execute("UPDATE delist_tasks SET status='done', done_at=datetime('now'), last_error=? WHERE id=?",
                       (f"cancelled: sale undone {why}".strip(), t["id"]))
            still_up.append(t["platform_id"])
    db.execute("UPDATE items SET status='listed' WHERE id=?", (item_id,))
    db.commit()
    return UndoResult(item, sale["platform_id"], still_up, removed)
