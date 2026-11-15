"""Print what's in the database right now (approvals, delist tasks, items).

    docker-compose exec storebot python bot/dbcheck.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))
from init_db import connect  # noqa: E402

con = connect()


def show(title, sql):
    rows = con.execute(sql).fetchall()
    print(f"\n== {title} ({len(rows)})")
    for r in rows:
        print("  " + " | ".join(f"{k}={r[k]}" for k in r.keys()))


show("approvals", "SELECT id, kind, status, decided_by, summary FROM approvals ORDER BY id DESC LIMIT 10")
show("delist tasks", """SELECT d.id, i.sku, l.platform_id, d.status FROM delist_tasks d
                        JOIN listings l ON l.id=d.listing_id JOIN items i ON i.id=l.item_id
                        ORDER BY d.id DESC LIMIT 10""")
show("items", "SELECT id, sku, name, status FROM items ORDER BY id DESC LIMIT 10")
