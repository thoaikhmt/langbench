import { Database } from "bun:sqlite";
import { Elysia, t } from "elysia";

const DB_PATH = process.env.DB_PATH ?? "app.db";
const HOST = "127.0.0.1";
const PORT = 8080;
const WORKERS = Number(process.env.WEB_WORKERS ?? 8);
const isWorker = process.env.BUN_WORKER === "1";

type Item = {
  id: number;
  name: string;
  description: string;
  price: number;
  created_at: string;
};

const itemBody = t.Object({
  name: t.String(),
  description: t.Optional(t.String()),
  price: t.Optional(t.Number()),
});

function start() {
  const db = new Database(DB_PATH);
  db.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA busy_timeout=5000;
    PRAGMA cache_size=-2000;
    PRAGMA mmap_size=268435456;
    PRAGMA temp_store=MEMORY;
  `);

  const COLS = "id, name, description, price, created_at";
  const selectById = db.query<Item, [number]>(
    `SELECT ${COLS} FROM items WHERE id = ?`,
  );
  const selectPage = db.query<Item, [number, number]>(
    `SELECT ${COLS} FROM items ORDER BY id LIMIT ? OFFSET ?`,
  );
  const insertItem = db.query<{ id: number }, [string, string, number]>(
    `INSERT INTO items(name, description, price, created_at)
     VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) RETURNING id`,
  );
  const updateItem = db.query<{ id: number }, [string, string, number, number]>(
    "UPDATE items SET name = ?, description = ?, price = ? WHERE id = ? RETURNING id",
  );
  const deleteItem = db.prepare("DELETE FROM items WHERE id = ?");

  const app = new Elysia()
    .get("/health", () => ({ status: "ok" }))
    .get("/items", ({ query, set }) => {
      const limit = Number(query.limit ?? 20);
      const offset = Number(query.offset ?? 0);
      if (!Number.isInteger(limit) || limit < 1 || !Number.isInteger(offset) || offset < 0) {
        set.status = 400;
        return { error: "invalid pagination" };
      }
      const capped = Math.min(limit, 100);
      return { items: selectPage.all(capped, offset), limit: capped, offset };
    })
    .get("/items/:id", ({ params, set }) => {
      const id = Number(params.id);
      if (!Number.isSafeInteger(id)) {
        set.status = 400;
        return { error: "invalid id" };
      }
      const item = selectById.get(id);
      if (!item) {
        set.status = 404;
        return { error: "not found" };
      }
      return item;
    })
    .post(
      "/items",
      ({ body, set }) => {
        if (body.name.length === 0) {
          set.status = 400;
          return { error: "name is required" };
        }
        const row = insertItem.get(
          body.name,
          body.description ?? "",
          body.price ?? 0,
        );
        set.status = 201;
        return selectById.get(row!.id);
      },
      { body: itemBody },
    )
    .put(
      "/items/:id",
      ({ params, body, set }) => {
        const id = Number(params.id);
        if (!Number.isSafeInteger(id)) {
          set.status = 400;
          return { error: "invalid id" };
        }
        if (body.name.length === 0) {
          set.status = 400;
          return { error: "name is required" };
        }
        const row = updateItem.get(
          body.name,
          body.description ?? "",
          body.price ?? 0,
          id,
        );
        if (!row) {
          set.status = 404;
          return { error: "not found" };
        }
        return selectById.get(id);
      },
      { body: itemBody },
    )
    .delete("/items/:id", ({ params, set }) => {
      const id = Number(params.id);
      if (!Number.isSafeInteger(id)) {
        set.status = 400;
        return { error: "invalid id" };
      }
      const info = deleteItem.run(id);
      if (info.changes === 0) {
        set.status = 404;
        return { error: "not found" };
      }
      set.status = 204;
      return new Response(null, { status: 204 });
    })
    .listen({ hostname: HOST, port: PORT, reusePort: true });

  return app;
}

if (!isWorker && WORKERS > 1) {
  const children: Bun.Subprocess[] = [];
  for (let i = 0; i < WORKERS; i++) {
    children.push(
      Bun.spawn([process.execPath, import.meta.path], {
        env: { ...process.env, BUN_WORKER: "1", WEB_WORKERS: "1" },
        stdout: "inherit",
        stderr: "inherit",
      }),
    );
  }
  const shutdown = () => {
    for (const child of children) child.kill();
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  await Promise.all(children.map((child) => child.exited));
  process.exit(0);
} else {
  start();
}
