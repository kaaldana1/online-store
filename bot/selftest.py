"""Put fake rows in the database so you can see the bot react. Run while bot.py is running:

    python bot/selftest.py            # one fake approval  -> appears in #approvals within 30 s
    python bot/selftest.py --alert    # one fake sale       -> delist alert appears in #alerts
    python bot/selftest.py --clean    # remove everything the self-test created
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))
from init_db import connect, init  # noqa: E402

con = connect()
init(con)
ap = argparse.ArgumentParser()
ap.add_argument("--alert", action="store_true")
ap.add_argument("--clean", action="store_true")
a = ap.parse_args()

if a.clean:
    con.execute("DELETE FROM approvals WHERE summary LIKE 'TEST:%'")
    ids = [r[0] for r in con.execute("SELECT id FROM items WHERE sku LIKE 'TEST-%'")]
    for i in ids:
        con.execute("DELETE FROM delist_tasks WHERE listing_id IN (SELECT id FROM listings WHERE item_id=?)", (i,))
        con.execute("DELETE FROM email_events WHERE item_id=?", (i,))
        con.execute("DELETE FROM sales WHERE item_id=?", (i,))
        con.execute("DELETE FROM items WHERE id=?", (i,))
    con.commit()
    print("Self-test rows removed.")
elif a.alert:
    n = con.execute("SELECT COUNT(*) FROM items WHERE sku LIKE 'TEST-%'").fetchone()[0] + 1
    item = con.execute("INSERT INTO items (sku, name, category, status) VALUES (?,?,'jewelry','listed')",
                       (f"TEST-{n:03d}", "TEST sterling cuff bracelet")).lastrowid
    ids = {p: con.execute("INSERT INTO listings (item_id, platform_id, status, list_price) VALUES (?,?,'live',45)",
                          (item, p)).lastrowid for p in ("ebay", "facebook", "poshmark")}
    con.execute("INSERT INTO sales (item_id, listing_id, sale_price, source) VALUES (?,?,45,'test')", (item, ids["ebay"]))
    con.commit()
    print(f"Fake sale of TEST-{n:03d} on eBay. Watch #alerts for Facebook + Poshmark delist alerts.")
else:
    con.execute("INSERT INTO approvals (kind, ref_table, ref_id, summary) VALUES "
                "('post_listing','listings',0,'TEST: post sterling cuff bracelet to eBay at $45 (net ~$33)')")
    con.commit()
    print("Fake approval added. Watch #approvals and press a button.")
