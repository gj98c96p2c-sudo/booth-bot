"""データベース接続とクエリ。"""

import asyncio
import datetime
import os
import aiohttp
import aiosqlite

import logging_utils as log

DB_PATH = "bot_data.db"
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")


def _turso_pipeline_url(database_url: str) -> str:
    """libsql:// URL を HTTPS pipeline URL に変換する。"""
    if database_url.startswith("libsql://"):
        database_url = "https://" + database_url[len("libsql://"):]
    return database_url.rstrip("/") + "/v2/pipeline"


def _convert_param(value):
    """Turso に送る前に値を型付きオブジェクトに変換する。"""
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, datetime.datetime):
        return {"type": "text", "value": value.isoformat()}
    if value is None:
        return {"type": "null"}
    return {"type": "text", "value": str(value)}


def _parse_cell(cell):
    """Turso が返すセルを Python の値に変換する。"""
    if not isinstance(cell, dict):
        return cell
    ctype = cell.get("type")
    value = cell.get("value")
    if ctype == "integer":
        return int(value)
    if ctype == "float":
        return float(value)
    if ctype == "null":
        return None
    return value


class TursoCursor:
    """Turso HTTP API の結果を aiosqlite っぽく使えるカーソル。"""

    def __init__(self, client: "TursoClient", sql: str, parameters=None):
        self._client = client
        self._sql = sql
        self._parameters = parameters
        self._result: dict | None = None
        self._cols: list[str] = []
        self._rows: list[list] = []
        self._index = 0
        self.rowcount = 0
        self.lastrowid = None
        self._fetch_task: asyncio.Task | None = None

    def _start_fetch(self):
        if self._fetch_task is None:
            self._fetch_task = asyncio.create_task(self._do_fetch())

    async def _do_fetch(self):
        result = await self._client._execute_request(self._sql, self._parameters)
        self._result = result
        self._cols = [col.get("name", "") for col in result.get("cols", [])]
        self._rows = result.get("rows", [])
        self._index = 0
        self.rowcount = result.get("affected_row_count", 0)
        self.lastrowid = result.get("last_insert_rowid")

    async def _ensure_loaded(self):
        self._start_fetch()
        if self._fetch_task is not None:
            await self._fetch_task

    def __await__(self):
        async def _resolve():
            await self._ensure_loaded()
            return self
        return _resolve().__await__()

    async def fetchone(self):
        await self._ensure_loaded()
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return tuple(_parse_cell(cell) for cell in row)

    async def fetchall(self):
        await self._ensure_loaded()
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return [tuple(_parse_cell(cell) for cell in row) for row in rows]

    async def __aenter__(self):
        await self._ensure_loaded()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TursoClient:
    """Turso データベースへの非同期 HTTP クライアント。"""

    def __init__(self, database_url: str, auth_token: str):
        self.url = _turso_pipeline_url(database_url)
        self.token = auth_token
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session:
            await self._session.close()
            self._session = None
        return False

    async def _execute_request(self, sql: str, parameters=None) -> dict:
        if self._session is None:
            raise RuntimeError("TursoClient は async with の中で使ってね")
        stmt: dict = {"sql": sql}
        if parameters:
            stmt["args"] = [_convert_param(p) for p in parameters]
        payload = {"requests": [{"type": "execute", "stmt": stmt}]}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with self._session.post(self.url, headers=headers, json=payload) as resp:
            data = await resp.json()
            result = data["results"][0]
            if result["type"] == "error":
                error = result.get("error", {})
                raise Exception(f"Turso error: {error}")
            return result["response"]["result"]

    def execute(self, sql: str, parameters=None):
        return TursoCursor(self, sql, parameters)

    async def commit(self):
        pass

    async def batch(self, sql_statements: list):
        if self._session is None:
            raise RuntimeError("TursoClient は async with の中で使ってね")
        requests = []
        for stmt in sql_statements:
            if isinstance(stmt, str):
                requests.append({"type": "execute", "stmt": {"sql": stmt}})
            else:
                sql, params = stmt
                requests.append({"type": "execute", "stmt": {"sql": sql, "args": [_convert_param(p) for p in params]}})
        payload = {"requests": requests}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with self._session.post(self.url, headers=headers, json=payload) as resp:
            return await resp.json()


def db_connect():
    """Turso が設定されていれば Turso、なければローカルの SQLite を使う。"""
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        log.info("db_connect", f"🗄️ データベース: Turso ({TURSO_DATABASE_URL})")
        return TursoClient(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)
    log.info("db_connect", f"🗄️ データベース: ローカルSQLite ({DB_PATH})")
    return aiosqlite.connect(DB_PATH)


async def init_db(db: aiosqlite.Connection) -> None:
    """DBテーブルを初期化する。既存テーブル互換を維持する。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            categories TEXT NOT NULL,
            allow_nsfw INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            price TEXT NOT NULL,
            category TEXT NOT NULL,
            likes INTEGER DEFAULT 0,
            image_url TEXT,
            is_adult INTEGER DEFAULT 0,
            published_at TIMESTAMP NOT NULL,
            shop_name TEXT,
            tags TEXT,
            notified_at TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            filter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            avatar_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            UNIQUE(channel_id, normalized_name)
        )
    """)
    try:
        await db.execute("ALTER TABLE filters ADD COLUMN normalized_name TEXT")
    except Exception:
        pass

    await db.execute("""
        CREATE TABLE IF NOT EXISTS shop_filters (
            filter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            shop_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            UNIQUE(channel_id, normalized_name)
        )
    """)
    # アバターフィルター（BOOTH商品ID基準）。表示用の名前も一緒に持つ
    await db.execute("""
        CREATE TABLE IF NOT EXISTS avatar_filters (
            filter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            avatar_item_id TEXT NOT NULL,
            avatar_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases TEXT,
            item_url TEXT,
            UNIQUE(channel_id, avatar_item_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS dm_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT,
            content TEXT NOT NULL,
            attachments TEXT,
            created_at TEXT NOT NULL,
            replied INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS dm_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
    """)
    await db.commit()
    await db.execute("""
        INSERT OR IGNORE INTO bot_state (key, value) VALUES ('failure_count', '0')
    """)
    await db.commit()


async def validate_db_connection() -> bool:
    """DB接続を確認し、永続化できているか検証する。"""
    try:
        async with db_connect() as db:
            await init_db(db)
            async with db.execute("SELECT value FROM bot_state WHERE key = 'failure_count'") as cursor:
                row = await cursor.fetchone()
                log.info("validate_db_connection", f"✅ DB接続テスト成功 (failure_count={row[0] if row else 'N/A'})")
                return True
    except Exception as e:
        log.error("validate_db_connection", f"❌ DB接続テスト失敗: {e}")
        return False


async def get_failure_count(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT value FROM bot_state WHERE key = 'failure_count'"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        await db.execute(
            "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('failure_count', '0')"
        )
        await db.commit()
        return 0
    try:
        return int(row[0] or 0)
    except ValueError:
        return 0


async def set_failure_count(db: aiosqlite.Connection, count: int) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('failure_count', ?)",
        (str(count),),
    )
    await db.commit()


async def load_channel_filters(db: aiosqlite.Connection, channel_id: int) -> list[tuple[str, str, str, str]]:
    """チャンネルのアバターフィルターを読み込む。

    Returns:
        [(avatar_item_id, avatar_name, normalized_name, aliases_json), ...]
    """
    async with db.execute(
        "SELECT avatar_item_id, avatar_name, normalized_name, aliases "
        "FROM avatar_filters WHERE channel_id = ?",
        (channel_id,),
    ) as cursor:
        return await cursor.fetchall()


async def count_legacy_name_filters(db: aiosqlite.Connection, channel_id: int) -> int:
    """旧「アバター名」フィルターの残存件数（移行案内用）。"""
    try:
        async with db.execute(
            "SELECT COUNT(*) FROM filters WHERE channel_id = ?", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


async def clear_legacy_name_filters(db: aiosqlite.Connection, channel_id: int) -> int:
    """旧「アバター名」フィルターを削除する。"""
    try:
        cursor = await db.execute("DELETE FROM filters WHERE channel_id = ?", (channel_id,))
        await db.commit()
        return cursor.rowcount or 0
    except Exception:
        return 0


async def load_channel_shop_filters(db: aiosqlite.Connection, channel_id: int) -> list[tuple[str, str]]:
    async with db.execute(
        "SELECT shop_name, normalized_name FROM shop_filters WHERE channel_id = ?",
        (channel_id,),
    ) as cursor:
        return await cursor.fetchall()


async def load_all_channel_filters(
    db: aiosqlite.Connection,
) -> tuple[dict[int, list[tuple[str, str, str, str]]], dict[int, list[tuple[str, str]]]]:
    """全チャンネルのフィルターをまとめて読み込む（通知処理の高速化用）。"""
    avatar_filters: dict[int, list[tuple[str, str, str, str]]] = {}
    async with db.execute(
        "SELECT channel_id, avatar_item_id, avatar_name, normalized_name, aliases FROM avatar_filters"
    ) as cursor:
        for row in await cursor.fetchall():
            avatar_filters.setdefault(row[0], []).append((row[1], row[2], row[3], row[4]))

    shop_filters: dict[int, list[tuple[str, str]]] = {}
    async with db.execute("SELECT channel_id, shop_name, normalized_name FROM shop_filters") as cursor:
        for row in await cursor.fetchall():
            shop_filters.setdefault(row[0], []).append((row[1], row[2]))

    return avatar_filters, shop_filters
