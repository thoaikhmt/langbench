package bench;

import io.javalin.Javalin;
import io.javalin.http.Context;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;

public final class Main {

    public record Item(long id, String name, String description, double price, String created_at) {}

    public static final class ItemInput {
        public String name;
        public String description = "";
        public double price;
    }

    /** Fixed-size connection pool. SQLite reads are cheap; writes serialize internally. */
    static final class Db {
        private final String url;
        private final BlockingQueue<Connection> pool;
        private final int size;

        Db(String path, int size) throws SQLException {
            this.url = "jdbc:sqlite:" + path;
            this.size = size;
            this.pool = new ArrayBlockingQueue<>(size);
            for (int i = 0; i < size; i++) {
                pool.add(open());
            }
        }

        private Connection open() throws SQLException {
            Connection c = DriverManager.getConnection(url);
            try (Statement st = c.createStatement()) {
                st.execute("PRAGMA journal_mode=WAL");
                st.execute("PRAGMA synchronous=NORMAL");
                st.execute("PRAGMA busy_timeout=5000");
                st.execute("PRAGMA cache_size=-2000");
                st.execute("PRAGMA mmap_size=268435456");
                st.execute("PRAGMA temp_store=MEMORY");
            }
            return c;
        }

        Connection take() {
            try {
                return pool.take();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(e);
            }
        }

        void give(Connection c) {
            if (c == null) {
                return;
            }
            try {
                if (c.isClosed()) {
                    pool.add(open());
                } else {
                    pool.add(c);
                }
            } catch (SQLException e) {
                try {
                    pool.add(open());
                } catch (SQLException ignored) {
                    // pool shrinks; benchmark continues
                }
            }
        }

        void close() {
            for (Connection c : pool) {
                try {
                    c.close();
                } catch (SQLException ignored) {
                }
            }
        }
    }

    private static final String COLS = "id, name, description, price, created_at";

    static Db db;

    static long parseId(Context ctx) {
        try {
            return Long.parseLong(ctx.pathParam("id"));
        } catch (NumberFormatException e) {
            ctx.status(400).json(Map.of("error", "invalid id"));
            return -1;
        }
    }

    static Item readItem(ResultSet rs) throws SQLException {
        return new Item(
                rs.getLong("id"),
                rs.getString("name"),
                rs.getString("description"),
                rs.getDouble("price"),
                rs.getString("created_at"));
    }

    static Item fetchItem(Connection c, long id) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT " + COLS + " FROM items WHERE id = ?")) {
            ps.setLong(1, id);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) {
                    return null;
                }
                return readItem(rs);
            }
        }
    }

    static int intParam(Context ctx, String name, int def, int min, int max) {
        String raw = ctx.queryParam(name);
        if (raw == null) {
            return def;
        }
        try {
            int v = Integer.parseInt(raw);
            if (v < min) {
                return Integer.MIN_VALUE;
            }
            return Math.min(v, max);
        } catch (NumberFormatException e) {
            return Integer.MIN_VALUE;
        }
    }

    static void jsonError(Context ctx, int status, String message) {
        ctx.status(status).json(Map.of("error", message));
    }

    public static void main(String[] args) throws Exception {
        String path = System.getenv().getOrDefault("DB_PATH", "app.db");
        int poolSize = Integer.parseInt(System.getenv().getOrDefault("DB_POOL", "16"));
        db = new Db(path, poolSize);

        Javalin app = Javalin.create(cfg -> {
            cfg.startup.showJavalinBanner = false;
            cfg.http.defaultContentType = "application/json";
            cfg.concurrency.useVirtualThreads = System.getenv("USE_VT") != null;
            if (System.getenv("JETTY_THREADS") != null) {
                cfg.jetty.threadPool = new org.eclipse.jetty.util.thread.QueuedThreadPool(
                        Integer.parseInt(System.getenv("JETTY_THREADS")));
            }
            cfg.events.serverStopped(() -> db.close());
            register(cfg.routes);
        });

        app.start("127.0.0.1", 8080);
    }

    static void register(io.javalin.router.JavalinDefaultRoutingApi r) {
        r.get("/health", ctx -> ctx.json(Map.of("status", "ok")));

        r.get("/items", ctx -> {
            int limit = intParam(ctx, "limit", 20, 1, 100);
            if (limit == Integer.MIN_VALUE) {
                jsonError(ctx, 400, "invalid limit");
                return;
            }
            int offset = intParam(ctx, "offset", 0, 0, Integer.MAX_VALUE);
            if (offset == Integer.MIN_VALUE) {
                jsonError(ctx, 400, "invalid offset");
                return;
            }

            List<Item> items = new ArrayList<>(limit);
            Connection c = db.take();
            try (PreparedStatement ps = c.prepareStatement(
                    "SELECT " + COLS + " FROM items ORDER BY id LIMIT ? OFFSET ?")) {
                ps.setInt(1, limit);
                ps.setInt(2, offset);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        items.add(readItem(rs));
                    }
                }
            } catch (SQLException e) {
                jsonError(ctx, 500, e.getMessage());
                return;
            } finally {
                db.give(c);
            }

            Map<String, Object> body = new LinkedHashMap<>();
            body.put("items", items);
            body.put("limit", limit);
            body.put("offset", offset);
            ctx.json(body);
        });

        r.get("/items/{id}", ctx -> {
            long id = parseId(ctx);
            if (id < 0) {
                return;
            }
            Item item;
            Connection c = db.take();
            try {
                item = fetchItem(c, id);
            } catch (SQLException e) {
                jsonError(ctx, 500, e.getMessage());
                return;
            } finally {
                db.give(c);
            }
            if (item == null) {
                jsonError(ctx, 404, "not found");
                return;
            }
            ctx.json(item);
        });

        r.post("/items", ctx -> {
            ItemInput in;
            try {
                in = ctx.bodyAsClass(ItemInput.class);
            } catch (Exception e) {
                jsonError(ctx, 400, "invalid body");
                return;
            }
            if (in.name == null || in.name.isEmpty()) {
                jsonError(ctx, 400, "name is required");
                return;
            }
            String now = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString();
            Item item;
            Connection c = db.take();
            try {
                long id;
                try (PreparedStatement ps = c.prepareStatement(
                        "INSERT INTO items(name, description, price, created_at) VALUES (?, ?, ?, ?)",
                        Statement.RETURN_GENERATED_KEYS)) {
                    ps.setString(1, in.name);
                    ps.setString(2, in.description);
                    ps.setDouble(3, in.price);
                    ps.setString(4, now);
                    ps.executeUpdate();
                    try (ResultSet keys = ps.getGeneratedKeys()) {
                        keys.next();
                        id = keys.getLong(1);
                    }
                }
                item = fetchItem(c, id);
            } catch (SQLException e) {
                jsonError(ctx, 500, e.getMessage());
                return;
            } finally {
                db.give(c);
            }
            ctx.status(201).json(item);
        });

        r.put("/items/{id}", ctx -> {
            long id = parseId(ctx);
            if (id < 0) {
                return;
            }
            ItemInput in;
            try {
                in = ctx.bodyAsClass(ItemInput.class);
            } catch (Exception e) {
                jsonError(ctx, 400, "invalid body");
                return;
            }
            if (in.name == null || in.name.isEmpty()) {
                jsonError(ctx, 400, "name is required");
                return;
            }
            Item item;
            Connection c = db.take();
            try {
                int changed;
                try (PreparedStatement ps = c.prepareStatement(
                        "UPDATE items SET name = ?, description = ?, price = ? WHERE id = ?")) {
                    ps.setString(1, in.name);
                    ps.setString(2, in.description);
                    ps.setDouble(3, in.price);
                    ps.setLong(4, id);
                    changed = ps.executeUpdate();
                }
                if (changed == 0) {
                    jsonError(ctx, 404, "not found");
                    return;
                }
                item = fetchItem(c, id);
            } catch (SQLException e) {
                jsonError(ctx, 500, e.getMessage());
                return;
            } finally {
                db.give(c);
            }
            ctx.json(item);
        });

        r.delete("/items/{id}", ctx -> {
            long id = parseId(ctx);
            if (id < 0) {
                return;
            }
            int changed;
            Connection c = db.take();
            try (PreparedStatement ps = c.prepareStatement("DELETE FROM items WHERE id = ?")) {
                ps.setLong(1, id);
                changed = ps.executeUpdate();
            } catch (SQLException e) {
                jsonError(ctx, 500, e.getMessage());
                return;
            } finally {
                db.give(c);
            }
            if (changed == 0) {
                jsonError(ctx, 404, "not found");
                return;
            }
            ctx.status(204);
        });

    }
}
