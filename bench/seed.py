#!/usr/bin/env python3
"""Create a deterministic seed SQLite database used by every implementation."""
import os
import sqlite3
import sys

ROWS = int(os.environ.get("SEED_ROWS", "10000"))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "seed.db")

if os.path.exists(OUT):
    os.remove(OUT)
for suffix in ("-wal", "-shm"):
    p = OUT + suffix
    if os.path.exists(p):
        os.remove(p)

con = sqlite3.connect(OUT)
con.executescript(
    """
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        price REAL NOT NULL,
        created_at TEXT NOT NULL
    );
    """
)
rows = [
    (
        f"item-{i}",
        f"description for item {i} " + ("x" * 80),
        round((i % 10000) * 0.01 + 1.0, 2),
        f"2026-01-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:00Z",
    )
    for i in range(1, ROWS + 1)
]
con.executemany(
    "INSERT INTO items(name, description, price, created_at) VALUES (?, ?, ?, ?)", rows
)
con.commit()
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()
print(f"seeded {ROWS} rows -> {OUT} ({os.path.getsize(OUT)} bytes)")
