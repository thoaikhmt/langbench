import os
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "app.db")

app = FastAPI(
    default_response_class=ORJSONResponse,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_local = threading.local()


def db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(
            DB_PATH, timeout=5.0, check_same_thread=False, isolation_level=None
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA cache_size=-2000")
        con.execute("PRAGMA mmap_size=268435456")
        con.execute("PRAGMA temp_store=MEMORY")
        _local.con = con
    return con


COLS = "id, name, description, price, created_at"
ITEM_FIELDS = ("id", "name", "description", "price", "created_at")


class ItemInput(BaseModel):
    name: str
    description: str = ""
    price: float = 0.0


def to_item(row) -> dict:
    return {k: row[k] for k in ITEM_FIELDS}


def fetch_item(item_id: int) -> dict:
    row = db().execute(
        f"SELECT {COLS} FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return to_item(row)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items")
def list_items(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
):
    rows = db().execute(
        f"SELECT {COLS} FROM items ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return {
        "items": [to_item(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@app.get("/items/{item_id}")
def get_item(item_id: int):
    return fetch_item(item_id)


@app.post("/items", status_code=201)
def create_item(payload: ItemInput):
    if not payload.name:
        raise HTTPException(status_code=400, detail="name is required")
    cur = db().execute(
        "INSERT INTO items(name, description, price, created_at) VALUES (?, ?, ?, ?)",
        (
            payload.name,
            payload.description,
            payload.price,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    return fetch_item(cur.lastrowid)


@app.put("/items/{item_id}")
def update_item(item_id: int, payload: ItemInput):
    if not payload.name:
        raise HTTPException(status_code=400, detail="name is required")
    cur = db().execute(
        "UPDATE items SET name = ?, description = ?, price = ? WHERE id = ?",
        (payload.name, payload.description, payload.price, item_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not found")
    return fetch_item(item_id)


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    cur = db().execute("DELETE FROM items WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not found")
    return None
