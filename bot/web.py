"""Storage map web server, running inside the bot process (same database, same event loop).

Open http://localhost:8765 on the computer running Docker.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "storage"))
sys.path.insert(0, str(ROOT / "listing"))
import search  # noqa: E402

PHOTOS = ROOT / "data" / "photos"
PAGE = ROOT / "web" / "storage.html"
ACTIVE = search.ACTIVE


def _photo_url(path: str | None) -> str | None:
    return f"/photos/{Path(path).name}" if path else None


def make_app(db) -> web.Application:
    routes = web.RouteTableDef()

    def item_rows(where: str = "1=1", args: tuple = ()) -> list[dict]:
        rows = db.execute(
            f"SELECT i.id, i.sku, i.name, i.bin_id, i.status, i.category, "
            f"(SELECT path FROM item_photos p WHERE p.item_id=i.id ORDER BY is_primary DESC, id LIMIT 1) AS photo "
            f"FROM items i WHERE i.status IN {ACTIVE} AND {where} ORDER BY i.id DESC", args).fetchall()
        return [{**dict(r), "photo": _photo_url(r["photo"])} for r in rows]

    @routes.get("/")
    async def index(_):
        return web.FileResponse(PAGE)

    @routes.get("/photos/{name}")
    async def photo(req):
        f = PHOTOS / Path(req.match_info["name"]).name     # basename only: no path tricks
        if not f.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(f)

    @routes.get("/api/state")
    async def state(_):
        bins = [dict(b) for b in db.execute("SELECT * FROM bins ORDER BY id")]
        return web.json_response({"bins": bins, "items": item_rows()})

    @routes.post("/api/bins")
    async def add_bin(req):
        b = await req.json()
        num = b.get("id") or (db.execute("SELECT COALESCE(MAX(id),0)+1 FROM bins").fetchone()[0])
        if db.execute("SELECT 1 FROM bins WHERE id=?", (num,)).fetchone():
            return web.json_response({"error": f"Bin {num} already exists"}, status=409)
        db.execute("INSERT INTO bins (id, label, x, y, w, h) VALUES (?,?,?,?,?,?)",
                   (num, b.get("label") or "", b.get("x", 0), b.get("y", 0), b.get("w", 2), b.get("h", 2)))
        db.commit()
        return web.json_response(dict(db.execute("SELECT * FROM bins WHERE id=?", (num,)).fetchone()))

    @routes.put("/api/bins/{id}")
    async def edit_bin(req):
        bid = int(req.match_info["id"])
        b = await req.json()
        cur = db.execute("SELECT * FROM bins WHERE id=?", (bid,)).fetchone()
        if not cur:
            raise web.HTTPNotFound()
        vals = {k: b.get(k, cur[k]) for k in ("label", "x", "y", "w", "h")}
        vals["w"], vals["h"] = max(1, vals["w"]), max(1, vals["h"])
        db.execute("UPDATE bins SET label=?, x=?, y=?, w=?, h=? WHERE id=?",
                   (vals["label"], vals["x"], vals["y"], vals["w"], vals["h"], bid))
        db.commit()
        return web.json_response({"id": bid, **vals})

    @routes.delete("/api/bins/{id}")
    async def del_bin(req):
        bid = int(req.match_info["id"])
        n = db.execute(f"SELECT COUNT(*) FROM items WHERE bin_id=? AND status IN {ACTIVE}", (bid,)).fetchone()[0]
        if n and req.query.get("force") != "1":
            return web.json_response({"error": f"Bin {bid} still has {n} item(s)", "count": n}, status=409)
        db.execute("UPDATE items SET bin_id=NULL WHERE bin_id=?", (bid,))
        db.execute("DELETE FROM bins WHERE id=?", (bid,))
        db.commit()
        return web.json_response({"ok": True})

    @routes.post("/api/items/{id}/bin")
    async def move_item(req):
        iid = int(req.match_info["id"])
        bid = (await req.json()).get("bin_id")
        if bid is not None and not db.execute("SELECT 1 FROM bins WHERE id=?", (bid,)).fetchone():
            return web.json_response({"error": f"No bin {bid}"}, status=404)
        db.execute("UPDATE items SET bin_id=? WHERE id=?", (bid, iid))
        db.commit()
        return web.json_response({"ok": True})

    @routes.post("/api/items")
    async def new_item(req):
        """Photo dropped from the computer straight onto a bin: create a quick item record."""
        form = await req.post()
        f = form.get("photo")
        if f is None or not getattr(f, "content_type", "").startswith("image/"):
            return web.json_response({"error": "Send an image file as 'photo'"}, status=400)
        name = (form.get("name") or Path(f.filename).stem)[:80]
        bid = int(form["bin_id"]) if form.get("bin_id") else None
        iid = db.execute("INSERT INTO items (sku, name, bin_id, sourced_at) VALUES (?,?,?,date('now'))",
                         (uuid.uuid4().hex, name, bid)).lastrowid
        sku = f"S{iid:04d}"
        PHOTOS.mkdir(parents=True, exist_ok=True)
        path = PHOTOS / f"{sku}-1{Path(f.filename).suffix.lower() or '.jpg'}"
        path.write_bytes(f.file.read())
        db.execute("UPDATE items SET sku=? WHERE id=?", (sku, iid))
        db.execute("INSERT INTO item_photos (item_id, path, is_primary) VALUES (?,?,1)",
                   (iid, str(path.relative_to(ROOT))))
        db.commit()
        return web.json_response(item_rows("i.id=?", (iid,))[0])

    @routes.patch("/api/items/{id}")
    async def rename_item(req):
        iid = int(req.match_info["id"])
        b = await req.json()
        if "name" in b:
            db.execute("UPDATE items SET name=? WHERE id=?", (str(b["name"])[:80], iid))
            db.commit()
        return web.json_response({"ok": True})

    @routes.get("/api/find")
    async def find(req):
        q = req.query.get("q", "").strip()
        if not q:
            return web.json_response([])
        if req.query.get("smart") == "1":
            # AI pass runs in a worker thread with its own DB connection.
            from init_db import connect
            res = await asyncio.to_thread(lambda: search.smart_find(connect(), q))
        else:
            res = search.find(db, q, limit=10)
        return web.json_response([{**r, "photo": _photo_url(r.get("photo"))} for r in res])

    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.add_routes(routes)
    return app


async def start(db, host: str = "0.0.0.0", port: int = 8765) -> web.AppRunner:
    runner = web.AppRunner(make_app(db))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"Storage map at http://localhost:{port}")
    return runner
