package main

import (
	"log"
	"os"
	"strconv"
	"sync"
	"time"

	"crawshaw.io/sqlite"
	"crawshaw.io/sqlite/sqlitex"
	"github.com/goccy/go-json"
	"github.com/gofiber/fiber/v3"
)

type ItemInput struct {
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Price       float64 `json:"price"`
}

const (
	selectItemSQL = "SELECT id, name, description, price, created_at FROM items WHERE id = ?1"
	selectListSQL = "SELECT id, name, description, price, created_at FROM items ORDER BY id LIMIT ?1 OFFSET ?2"
	insertSQL     = "INSERT INTO items(name, description, price, created_at) VALUES (?1, ?2, ?3, ?4)"
	updateSQL     = "UPDATE items SET name = ?1, description = ?2, price = ?3 WHERE id = ?4"
	createdSQL    = "SELECT created_at FROM items WHERE id = ?1"
	deleteSQL     = "DELETE FROM items WHERE id = ?1"
)

type connPool struct {
	ch    chan *sqlite.Conn
	conns []*sqlite.Conn
}

func openPool(path string, size int) (*connPool, error) {
	flags := sqlite.SQLITE_OPEN_READWRITE | sqlite.SQLITE_OPEN_CREATE |
		sqlite.SQLITE_OPEN_WAL | sqlite.SQLITE_OPEN_NOMUTEX |
		sqlite.SQLITE_OPEN_PRIVATECACHE
	p := &connPool{ch: make(chan *sqlite.Conn, size), conns: make([]*sqlite.Conn, 0, size)}
	for i := 0; i < size; i++ {
		c, err := sqlite.OpenConn(path, flags)
		if err != nil {
			p.Close()
			return nil, err
		}
		pragmas := []string{
			"PRAGMA journal_mode=WAL",
			"PRAGMA synchronous=NORMAL",
			"PRAGMA busy_timeout=5000",
			"PRAGMA cache_size=-8192",
			"PRAGMA mmap_size=268435456",
			"PRAGMA temp_store=MEMORY",
		}
		if v := os.Getenv("WAL_AC"); v != "" {
			pragmas = append(pragmas, "PRAGMA wal_autocheckpoint="+v)
		}
		for _, pragma := range pragmas {
			if err := sqlitex.Exec(c, pragma, nil); err != nil {
				c.Close()
				p.Close()
				return nil, err
			}
		}
		p.conns = append(p.conns, c)
		p.ch <- c
	}
	return p, nil
}

func (p *connPool) Get() *sqlite.Conn  { return <-p.ch }
func (p *connPool) Put(c *sqlite.Conn) { p.ch <- c }
func (p *connPool) Close() {
	for _, c := range p.conns {
		_ = c.Close()
	}
}

var (
	readPool  *connPool
	writePool *connPool

	hex     = "0123456789abcdef"
	bufPool = sync.Pool{New: func() any {
		b := make([]byte, 0, 8192)
		return &b
	}}
)

func appendJSONString(b, s []byte) []byte {
	b = append(b, '"')
	start := 0
	for i := 0; i < len(s); i++ {
		ch := s[i]
		if ch >= 0x20 && ch != '"' && ch != '\\' {
			continue
		}
		b = append(b, s[start:i]...)
		switch ch {
		case '"':
			b = append(b, '\\', '"')
		case '\\':
			b = append(b, '\\', '\\')
		case '\n':
			b = append(b, '\\', 'n')
		case '\r':
			b = append(b, '\\', 'r')
		case '\t':
			b = append(b, '\\', 't')
		case '\b':
			b = append(b, '\\', 'b')
		case '\f':
			b = append(b, '\\', 'f')
		default:
			b = append(b, '\\', 'u', '0', '0', hex[ch>>4], hex[ch&0xF])
		}
		start = i + 1
	}
	b = append(b, s[start:]...)
	b = append(b, '"')
	return b
}

func columnBytes(stmt *sqlite.Stmt, col int, scratch *[2048]byte) []byte {
	n := stmt.ColumnLen(col)
	if n == 0 {
		return nil
	}
	var b []byte
	if n <= len(scratch) {
		b = scratch[:n]
	} else {
		b = make([]byte, n)
	}
	stmt.ColumnBytes(col, b)
	return b
}

func appendItemFromStmt(b []byte, stmt *sqlite.Stmt) []byte {
	var scratch [2048]byte
	b = append(b, `{"id":`...)
	b = strconv.AppendInt(b, stmt.ColumnInt64(0), 10)
	b = append(b, `,"name":`...)
	b = appendJSONString(b, columnBytes(stmt, 1, &scratch))
	b = append(b, `,"description":`...)
	b = appendJSONString(b, columnBytes(stmt, 2, &scratch))
	b = append(b, `,"price":`...)
	b = strconv.AppendFloat(b, stmt.ColumnFloat(3), 'f', -1, 64)
	b = append(b, `,"created_at":`...)
	b = appendJSONString(b, columnBytes(stmt, 4, &scratch))
	b = append(b, '}')
	return b
}

func appendItem(b []byte, id int64, name, description string, price float64, created string) []byte {
	b = append(b, `{"id":`...)
	b = strconv.AppendInt(b, id, 10)
	b = append(b, `,"name":`...)
	b = appendJSONString(b, []byte(name))
	b = append(b, `,"description":`...)
	b = appendJSONString(b, []byte(description))
	b = append(b, `,"price":`...)
	b = strconv.AppendFloat(b, price, 'f', -1, 64)
	b = append(b, `,"created_at":`...)
	b = appendJSONString(b, []byte(created))
	b = append(b, '}')
	return b
}

func acquireBuf() (*[]byte, []byte) {
	bp := bufPool.Get().(*[]byte)
	return bp, (*bp)[:0]
}

func releaseBuf(bp *[]byte, buf []byte) {
	*bp = buf
	bufPool.Put(bp)
}

func parseID(c fiber.Ctx) (int64, bool) {
	id, err := strconv.ParseInt(c.Params("id"), 10, 64)
	if err != nil {
		_ = c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid id"})
		return 0, false
	}
	return id, true
}

func sendJSON(c fiber.Ctx, buf []byte) error {
	c.Set("Content-Type", "application/json")
	c.Response().AppendBody(buf)
	return nil
}

func main() {
	dbPath := os.Getenv("DB_PATH")
	if dbPath == "" {
		dbPath = "app.db"
	}
	readSize := 16
	if v := os.Getenv("DB_POOL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			readSize = n
		}
	}
	writeSize := 1
	if v := os.Getenv("DB_WRITE_POOL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			writeSize = n
		}
	}

	var err error
	if readPool, err = openPool(dbPath, readSize); err != nil {
		log.Fatal(err)
	}
	if writePool, err = openPool(dbPath, writeSize); err != nil {
		log.Fatal(err)
	}

	app := fiber.New(fiber.Config{
		JSONEncoder: json.Marshal,
		JSONDecoder: json.Unmarshal,
	})

	app.Get("/health", func(c fiber.Ctx) error {
		return c.JSON(fiber.Map{"status": "ok"})
	})

	app.Get("/items", func(c fiber.Ctx) error {
		limit, err := strconv.Atoi(c.Query("limit", "20"))
		if err != nil || limit < 1 {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid limit"})
		}
		if limit > 100 {
			limit = 100
		}
		offset, err := strconv.Atoi(c.Query("offset", "0"))
		if err != nil || offset < 0 {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid offset"})
		}

		conn := readPool.Get()
		defer readPool.Put(conn)
		stmt, err := conn.Prepare(selectListSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		defer stmt.Reset()
		stmt.BindInt64(1, int64(limit))
		stmt.BindInt64(2, int64(offset))

		bp, buf := acquireBuf()
		defer func() { releaseBuf(bp, buf) }()
		buf = append(buf, `{"items":[`...)
		first := true
		for {
			row, err := stmt.Step()
			if err != nil {
				return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
			}
			if !row {
				break
			}
			if !first {
				buf = append(buf, ',')
			}
			first = false
			buf = appendItemFromStmt(buf, stmt)
		}
		buf = append(buf, `],"limit":`...)
		buf = strconv.AppendInt(buf, int64(limit), 10)
		buf = append(buf, `,"offset":`...)
		buf = strconv.AppendInt(buf, int64(offset), 10)
		buf = append(buf, '}')
		return sendJSON(c, buf)
	})

	app.Get("/items/:id", func(c fiber.Ctx) error {
		id, ok := parseID(c)
		if !ok {
			return nil
		}
		conn := readPool.Get()
		defer readPool.Put(conn)
		stmt, err := conn.Prepare(selectItemSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		defer stmt.Reset()
		stmt.BindInt64(1, id)
		row, err := stmt.Step()
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		if !row {
			return c.Status(fiber.StatusNotFound).JSON(fiber.Map{"error": "not found"})
		}
		bp, buf := acquireBuf()
		defer func() { releaseBuf(bp, buf) }()
		buf = appendItemFromStmt(buf, stmt)
		return sendJSON(c, buf)
	})

	app.Post("/items", func(c fiber.Ctx) error {
		var in ItemInput
		if err := c.Bind().JSON(&in); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid body"})
		}
		if in.Name == "" {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "name is required"})
		}
		created := time.Now().UTC().Format(time.RFC3339)
		conn := writePool.Get()
		defer writePool.Put(conn)
		stmt, err := conn.Prepare(insertSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		defer stmt.Reset()
		stmt.BindText(1, in.Name)
		stmt.BindText(2, in.Description)
		stmt.BindFloat(3, in.Price)
		stmt.BindText(4, created)
		if _, err := stmt.Step(); err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		id := conn.LastInsertRowID()
		bp, buf := acquireBuf()
		defer func() { releaseBuf(bp, buf) }()
		buf = appendItem(buf, id, in.Name, in.Description, in.Price, created)
		c.Status(fiber.StatusCreated)
		return sendJSON(c, buf)
	})

	app.Put("/items/:id", func(c fiber.Ctx) error {
		id, ok := parseID(c)
		if !ok {
			return nil
		}
		var in ItemInput
		if err := c.Bind().JSON(&in); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid body"})
		}
		if in.Name == "" {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "name is required"})
		}
		conn := writePool.Get()
		defer writePool.Put(conn)
		stmt, err := conn.Prepare(updateSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		defer stmt.Reset()
		stmt.BindText(1, in.Name)
		stmt.BindText(2, in.Description)
		stmt.BindFloat(3, in.Price)
		stmt.BindInt64(4, id)
		if _, err := stmt.Step(); err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		if conn.Changes() == 0 {
			return c.Status(fiber.StatusNotFound).JSON(fiber.Map{"error": "not found"})
		}
		stmt2, err := conn.Prepare(createdSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		stmt2.BindInt64(1, id)
		row, err := stmt2.Step()
		if err != nil {
			_ = stmt2.Reset()
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		var created string
		if row {
			created = stmt2.ColumnText(0)
		}
		_ = stmt2.Reset()
		bp, buf := acquireBuf()
		defer func() { releaseBuf(bp, buf) }()
		buf = appendItem(buf, id, in.Name, in.Description, in.Price, created)
		return sendJSON(c, buf)
	})

	app.Delete("/items/:id", func(c fiber.Ctx) error {
		id, ok := parseID(c)
		if !ok {
			return nil
		}
		conn := writePool.Get()
		defer writePool.Put(conn)
		stmt, err := conn.Prepare(deleteSQL)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		defer stmt.Reset()
		stmt.BindInt64(1, id)
		if _, err := stmt.Step(); err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
		if conn.Changes() == 0 {
			return c.Status(fiber.StatusNotFound).JSON(fiber.Map{"error": "not found"})
		}
		return c.SendStatus(fiber.StatusNoContent)
	})

	log.Fatal(app.Listen("127.0.0.1:8080", fiber.ListenConfig{DisableStartupMessage: true}))
}
