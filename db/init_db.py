"""Create (or upgrade) the store database and seed platforms from fees.toml.

    python db/init_db.py            # creates data/store.db
    python db/init_db.py --demo     # also adds a demo item listed on 3 platforms and simulates a sale
"""
import argparse
import sqlite3
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "store.db"


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add them to an
# existing database, so they're added here when missing.
ADDED_COLUMNS = [
    ("sales", "source", "TEXT"),
    ("sales", "voided_at", "TEXT"),
]


def init(con: sqlite3.Connection) -> None:
    for table, col, decl in ADDED_COLUMNS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.executescript((ROOT / "db" / "schema.sql").read_text())
    fees = tomllib.loads((ROOT / "platforms" / "fees.toml").read_text())
    for pid, p in fees.items():
        if pid == "meta":
            continue
        con.execute(
            "INSERT INTO platforms (id, name, priority, automation, enabled) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, priority=excluded.priority, "
            "automation=excluded.automation, enabled=excluded.enabled",
            (pid, p["name"], p.get("priority", 2), p.get("automation", "manual"), int(p.get("enabled", False))),
        )
    con.commit()


def demo(con: sqlite3.Connection) -> None:
    con.execute("INSERT OR IGNORE INTO bins (id, label, x, y, w, h) VALUES (1,'Top shelf left',0,0,2,1)")
    cur = con.execute(
        "INSERT INTO items (sku, name, category, cost_basis, bin_id, status) "
        "VALUES ('J-0001','sterling silver cuff bracelet, floral','jewelry',3,1,'listed')")
    item = cur.lastrowid
    ids = {}
    for pid, price in (("ebay", 45), ("facebook", 40), ("poshmark", 48)):
        ids[pid] = con.execute(
            "INSERT INTO listings (item_id, platform_id, status, list_price, posted_at) "
            "VALUES (?,?,'live',?,datetime('now'))", (item, pid, price)).lastrowid
    con.execute("INSERT INTO sales (item_id, listing_id, sale_price) VALUES (?,?,40)", (item, ids["facebook"]))
    con.commit()
    print("Demo: sold J-0001 on Facebook. Delist tasks created automatically:")
    for r in con.execute(
            "SELECT d.id, l.platform_id, d.status FROM delist_tasks d JOIN listings l ON l.id=d.listing_id"):
        print(f"  task {r['id']}: {r['platform_id']:<9} -> {r['status']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args()
    con = connect(a.db)
    init(con)
    print(f"Database ready at {a.db}")
    if a.demo:
        demo(con)
    sys.exit(0)
