use std::env;

use actix_web::{web, App, HttpResponse, HttpServer};
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use rusqlite::{params, Row};
use serde::{Deserialize, Serialize};
use serde_json::json;

type DbPool = Pool<SqliteConnectionManager>;


#[derive(Serialize)]
struct Item {
    id: i64,
    name: String,
    description: String,
    price: f64,
    created_at: String,
}

#[derive(Deserialize)]
struct ItemInput {
    name: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    price: f64,
}

#[derive(Serialize)]
struct ItemList {
    items: Vec<Item>,
    limit: i64,
    offset: i64,
}

#[derive(Deserialize)]
struct ListQuery {
    limit: Option<i64>,
    offset: Option<i64>,
}

fn row_to_item(row: &Row<'_>) -> rusqlite::Result<Item> {
    Ok(Item {
        id: row.get(0)?,
        name: row.get(1)?,
        description: row.get(2)?,
        price: row.get(3)?,
        created_at: row.get(4)?,
    })
}

fn fetch_item(conn: &rusqlite::Connection, id: i64) -> Result<Option<Item>, String> {
    let mut stmt = conn
        .prepare_cached("SELECT id, name, description, price, created_at FROM items WHERE id = ?1")
        .map_err(|e| e.to_string())?;
    let mut rows = stmt.query(params![id]).map_err(|e| e.to_string())?;
    match rows.next().map_err(|e| e.to_string())? {
        Some(row) => Ok(Some(row_to_item(row).map_err(|e| e.to_string())?)),
        None => Ok(None),
    }
}

fn err_response(e: String) -> HttpResponse {
    HttpResponse::InternalServerError().json(json!({ "error": e }))
}

async fn health() -> HttpResponse {
    HttpResponse::Ok().json(json!({ "status": "ok" }))
}

async fn get_item(pool: web::Data<DbPool>, path: web::Path<i64>) -> HttpResponse {
    let id = path.into_inner();
    let conn = match pool.get() {
        Ok(c) => c,
        Err(e) => return err_response(e.to_string()),
    };
    match fetch_item(&conn, id) {
        Ok(Some(item)) => HttpResponse::Ok().json(item),
        Ok(None) => HttpResponse::NotFound().json(json!({ "error": "not found" })),
        Err(e) => err_response(e),
    }
}

async fn list_items(pool: web::Data<DbPool>, q: web::Query<ListQuery>) -> HttpResponse {
    let mut limit = q.limit.unwrap_or(20);
    let offset = q.offset.unwrap_or(0);
    if limit < 1 || offset < 0 {
        return HttpResponse::BadRequest().json(json!({ "error": "invalid pagination" }));
    }
    if limit > 100 {
        limit = 100;
    }

    let conn = match pool.get() {
        Ok(c) => c,
        Err(e) => return err_response(e.to_string()),
    };
    let mut stmt = match conn.prepare_cached(
        "SELECT id, name, description, price, created_at FROM items ORDER BY id LIMIT ?1 OFFSET ?2",
    ) {
        Ok(s) => s,
        Err(e) => return err_response(e.to_string()),
    };
    let rows = match stmt.query_map(params![limit, offset], row_to_item) {
        Ok(r) => r,
        Err(e) => return err_response(e.to_string()),
    };
    let mut items = Vec::with_capacity(limit as usize);
    for row in rows {
        match row {
            Ok(item) => items.push(item),
            Err(e) => return err_response(e.to_string()),
        }
    }
    HttpResponse::Ok().json(ItemList { items, limit, offset })
}

async fn create_item(pool: web::Data<DbPool>, body: web::Json<ItemInput>) -> HttpResponse {
    let input = body.into_inner();
    if input.name.is_empty() {
        return HttpResponse::BadRequest().json(json!({ "error": "name is required" }));
    }

    let pool = pool.get_ref().clone();
    let res = web::block(move || -> Result<Item, String> {
        let conn = pool.get().map_err(|e| e.to_string())?;
        conn.execute(
            "INSERT INTO items(name, description, price, created_at)
             VALUES (?1, ?2, ?3, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            params![input.name, input.description, input.price],
        )
        .map_err(|e| e.to_string())?;
        let id = conn.last_insert_rowid();
        fetch_item(&conn, id)?.ok_or_else(|| "insert failed".to_string())
    })
    .await;

    match res {
        Ok(Ok(item)) => HttpResponse::Created().json(item),
        Ok(Err(e)) => err_response(e),
        Err(e) => err_response(e.to_string()),
    }
}

async fn update_item(
    pool: web::Data<DbPool>,
    path: web::Path<i64>,
    body: web::Json<ItemInput>,
) -> HttpResponse {
    let id = path.into_inner();
    let input = body.into_inner();
    if input.name.is_empty() {
        return HttpResponse::BadRequest().json(json!({ "error": "name is required" }));
    }

    let pool = pool.get_ref().clone();
    let res = web::block(move || -> Result<Option<Item>, String> {
        let conn = pool.get().map_err(|e| e.to_string())?;
        let changed = conn
            .execute(
                "UPDATE items SET name = ?1, description = ?2, price = ?3 WHERE id = ?4",
                params![input.name, input.description, input.price, id],
            )
            .map_err(|e| e.to_string())?;
        if changed == 0 {
            return Ok(None);
        }
        fetch_item(&conn, id)
    })
    .await;

    match res {
        Ok(Ok(Some(item))) => HttpResponse::Ok().json(item),
        Ok(Ok(None)) => HttpResponse::NotFound().json(json!({ "error": "not found" })),
        Ok(Err(e)) => err_response(e),
        Err(e) => err_response(e.to_string()),
    }
}

async fn delete_item(pool: web::Data<DbPool>, path: web::Path<i64>) -> HttpResponse {
    let id = path.into_inner();
    let pool = pool.get_ref().clone();
    let res = web::block(move || -> Result<bool, String> {
        let conn = pool.get().map_err(|e| e.to_string())?;
        let changed = conn
            .execute("DELETE FROM items WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
        Ok(changed > 0)
    })
    .await;

    match res {
        Ok(Ok(true)) => HttpResponse::NoContent().finish(),
        Ok(Ok(false)) => HttpResponse::NotFound().json(json!({ "error": "not found" })),
        Ok(Err(e)) => err_response(e),
        Err(e) => err_response(e.to_string()),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let path = env::var("DB_PATH").unwrap_or_else(|_| "app.db".to_string());
    let manager = SqliteConnectionManager::file(&path).with_init(|c| {
        c.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=NORMAL;
             PRAGMA busy_timeout=5000;
             PRAGMA cache_size=-2000;
             PRAGMA mmap_size=268435456;
             PRAGMA temp_store=MEMORY;",
        )
    });
    let pool = Pool::builder()
        .max_size(32)
        .min_idle(Some(8))
        .build(manager)
        .expect("failed to create SQLite pool");

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/health", web::get().to(health))
            .route("/items", web::get().to(list_items))
            .route("/items", web::post().to(create_item))
            .route("/items/{id}", web::get().to(get_item))
            .route("/items/{id}", web::put().to(update_item))
            .route("/items/{id}", web::delete().to(delete_item))
    })
    .workers(8)
    .bind(("127.0.0.1", 8080))?
    .run()
    .await
}
