"""
BOOTH VRChat 新作通知 Bot
- BOOTH JSON API を使用
- published_at で新作判定
- チャンネルごとにカテゴリ / アバター名フィルター / R-18 設定
"""

import asyncio
import datetime
import json
import os
import re
import unicodedata
from typing import Literal

import aiohttp
from aiohttp import web
import aiosqlite
import bs4
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────────────────────────
# 定数
# ───────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = "bot_data.db"
ADMIN_CHANNEL_ID_RAW = os.getenv("ADMIN_CHANNEL_ID", "")
ADMIN_CHANNEL_ID = int(ADMIN_CHANNEL_ID_RAW) if ADMIN_CHANNEL_ID_RAW.strip().lstrip("-").isdigit() else None

BOT_TEST_CHANNEL_ID_RAW = os.getenv("BOT_TEST_CHANNEL_ID", "")
BOT_TEST_CHANNEL_ID = int(BOT_TEST_CHANNEL_ID_RAW) if BOT_TEST_CHANNEL_ID_RAW.strip().lstrip("-").isdigit() else None

CATEGORY_LABELS = {
    "衣装": ["3D衣装"],
    "髪": ["3D髪型", "3D髪"],
    "小物": ["3D装飾品", "3D小道具"],
    "ギミック": ["3Dツール・システム", "3Dモーション・アニメーション", "3D小道具"],
}

# VRChatタグ付き商品の新着検索URL（カテゴリはJSON内の category.name で判定）
SEARCH_URL_TEMPLATE = "https://booth.pm/ja/search/VRChat?sort=new&page={page}"

SEARCH_PAGES = 2                # 巡回するページ数（1ページ60件）
CHECK_INTERVAL_MINUTES = 5      # 巡回間隔（分）
LOOKBACK_MINUTES = 10           # 何分以内に公開された商品を「新作」とみなすか
MAX_RETRIES = 3                 # HTTPリトライ回数
RETRY_BASE_DELAY = 2            # リトライの基底秒数
FAILURE_ALERT_THRESHOLD = 3     # 何回連続で失敗したら警告を出すか

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
    """Turso HTTP API の結果を aiosqlite っぽく使えるカーソル。

    aiosqlite と同じく ``async with db.execute(...) as cursor:`` でも
    ``cursor = await db.execute(...)`` でも使えるよう、
    ``execute`` 自体は同期メソッドとして即座にカーソルを返し、
    実際の HTTP リクエストはカーソルが使われるタイミングで await する。
    """

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
        """await db.execute(...) でも使えるようにする。"""
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
        """実際に Turso にリクエストし、生の result 辞書を返す。"""
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
        """aiosqlite 風に同期的にカーソルを返す。"""
        return TursoCursor(self, sql, parameters)

    async def commit(self):
        """Turso HTTP API は各リクエストが自動コミットなので何もしない。"""
        pass

    async def batch(self, sql_statements: list):
        """複数のSQLをまとめて実行する（テーブル作成用）。"""
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
        return TursoClient(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)
    return aiosqlite.connect(DB_PATH)


# カテゴリ名からユーザー向けラベル（衣装/髪/小物/ギミック）を返す
def map_category_label(category_name: str) -> str | None:
    for label, booth_names in CATEGORY_LABELS.items():
        if category_name in booth_names:
            return label
    return None


# 商品に対応する通知カテゴリのリストを返す（無料は追加）
def get_item_labels(item: dict) -> list[str]:
    labels: list[str] = []
    base_label = map_category_label(item["category_name"])
    if base_label:
        labels.append(base_label)
    if item["price"] == "¥ 0":
        labels.append("無料")
    return labels

intents = discord.Intents.default()


# ───────────────────────────────────────────
# 文字列正規化（アバター名フィルター用）
# ───────────────────────────────────────────
_KATAKANA_TO_HIRAGANA = str.maketrans(
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン"
    "ガギグゲゴザジズゼゾダヂヅデドバビブベボパピプペポ"
    "ァィゥェォャュョッヮ",
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
    "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽ"
    "ぁぃぅぇぉゃゅょっゎ",
)


def normalize_avatar_name(name: str) -> str:
    """
    アバター名を比較用に正規化する。
    大文字小文字・ひらがなカタカナ・全角半角・記号空白を同一視する。
    """
    # 小文字化
    name = name.lower()
    # Unicode正規化（全角英数字→半角 など）
    name = unicodedata.normalize("NFKC", name)
    # カタカナ → ひらがな
    name = name.translate(_KATAKANA_TO_HIRAGANA)
    # 記号・空白を除去
    name = re.sub(r"[^\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\u3400-\u4dbf\w]", "", name)
    return name


# ───────────────────────────────────────────
# Bot 本体
# ───────────────────────────────────────────
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.init_db()
        await self.tree.sync()
        print("✅ スラッシュコマンドの同期が完了しました")
        check_booth_job.start()

    async def init_db(self):
        """DBテーブルを初期化する。既存テーブル互換を維持する。"""
        async with db_connect() as db:
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
            # 後方互換: normalized_name カラムが無い場合は追加
            try:
                await db.execute("ALTER TABLE filters ADD COLUMN normalized_name TEXT")
            except Exception:
                pass

            # 自己申告用の状態管理テーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.commit()

            # bot_state に初期値を入れる
            await db.execute("""
                INSERT OR IGNORE INTO bot_state (key, value) VALUES ('failure_count', '0')
            """)
            await db.commit()


# 連続失敗回数をDBから取得する
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


# 連続失敗回数をDBに保存する
async def set_failure_count(db: aiosqlite.Connection, count: int) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('failure_count', ?)",
        (str(count),),
    )
    await db.commit()


# 管理用チャンネルに詳細警告を送る
async def send_admin_alert(title: str, description: str, color: int = 0xFF0000) -> None:
    if ADMIN_CHANNEL_ID is None:
        print(f"⚠️ ADMIN_CHANNEL_ID が未設定なので管理用警告をスキップ: {title}")
        return

    channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ADMIN_CHANNEL_ID)
        except Exception as e:
            print(f"❌ 管理用チャンネル取得失敗 (ID: {ADMIN_CHANNEL_ID}): {e}")
            return

    if channel is None:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text="BOOTH通知Bot 自己申告システム")

    try:
        await channel.send(embed=embed)
        print(f"🚨 管理用チャンネルに警告を送信: {title}")
    except Exception as e:
        print(f"❌ 管理用チャンネルへの警告送信失敗: {e}")


# 登録済み全チャンネルにユーザー向け告知を送る
async def send_user_outage_notice(db: aiosqlite.Connection, message_text: str) -> None:
    async with db.execute("SELECT channel_id FROM channels") as cursor:
        rows = await cursor.fetchall()

    if not rows:
        print("ℹ️ 通知設定されているチャンネルがないのでユーザー告知をスキップ")
        return

    for (channel_id,) in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                print(f"🗑️ 存在しないチャンネル (ID: {channel_id}) をDBから削除")
                await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
                await db.commit()
                continue
            except Exception as e:
                print(f"❌ チャンネル取得失敗 (ID: {channel_id}): {e}")
                continue

        if channel is None:
            continue

        try:
            await channel.send(message_text)
            print(f"📢 ユーザー告知送信: #{channel.name}")
        except discord.Forbidden:
            print(f"❌ ユーザー告知送信権限なし: #{channel.name} (ID: {channel_id})")
        except Exception as e:
            print(f"❌ ユーザー告知送信失敗: #{channel.name}: {e}")


bot = MyBot()


# ───────────────────────────────────────────
# Web サーバー（Render用）
# ───────────────────────────────────────────
async def handle_ping(request):
    return web.Response(text="BOOTH Bot is alive!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Webサーバーがポート {port} で起動しました")


# ───────────────────────────────────────────
# サーバー入室メッセージ
# ───────────────────────────────────────────
@bot.event
async def on_guild_join(guild: discord.Guild):
    target_channel = guild.system_channel
    if target_channel is None or not target_channel.permissions_for(guild.me).send_messages:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break
    if target_channel is None:
        return

    embed = discord.Embed(
        title="🎉 BOOTH通知Bot が導入されました！",
        description="導入ありがとうございます！BOOTHのVRChat向け新作アイテムを自動でお知らせします！",
        color=0xFF6473,
    )
    embed.add_field(
        name="📌 基本コマンド（使い方）",
        value=(
            "`/set-channel` ➔ 通知チャンネルとジャンルを設定\n"
            "`/remove-channel` ➔ 通知設定を解除\n"
            "`/filter add` ➔ 通知したいアバター名を追加\n"
            "`/filter remove` ➔ フィルターを削除\n"
            "`/filter list` ➔ 登録済みフィルター一覧\n"
            "`/set-nsfw allow/deny` ➔ R-18の表示/非表示を切り替え\n"
            "`/status` ➔ 現在の設定を確認"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 それぞれのコマンドでできること",
        value=(
            "**`/set-channel`**\n"
            "➔ 今のチャンネルをBOOTH通知専用にする。\n"
            "　例：衣装と無料だけ通知したい → 衣装・無料にチェック\n\n"
            "**`/filter add アバター名`**\n"
            "➔ 特定のアバター名が入った商品だけ通知してほしいときに使う。\n"
            "　例：`/filter add セレスティア` → タグやタイトルに『セレスティア』と入った商品だけ通知\n\n"
            "**`/filter remove アバター名`**\n"
            "➔ 登録したアバター名フィルターを削除する。\n"
            "　例：`/filter remove セレスティア`\n\n"
            "**`/filter list`**\n"
            "➔ 今のチャンネルに登録されているフィルター一覧を見る。\n\n"
            "**`/set-nsfw allow`**\n"
            "➔ R-18商品も通知する。デフォルトでは非表示。\n\n"
            "**`/set-nsfw deny`**\n"
            "➔ R-18商品を非表示にする（初期状態）。\n\n"
            "**`/remove-channel`**\n"
            "➔ このチャンネルへのBOOTH通知を完全に止める。\n\n"
            "**`/status`**\n"
            "➔ 今のチャンネルで何が設定されてるか確認できる。"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ 初期設定の手順",
        value=(
            "1. `/set-channel` で通知チャンネルとジャンルを設定\n"
            "2. （任意）`/filter add` で通知したいアバター名を登録\n"
            "3. （任意）`/set-nsfw` でR-18の表示/非表示を切り替え"
        ),
        inline=False,
    )
    embed.add_field(
        name="📝 このBotの動き",
        value=(
            "• 5分に1回BOOTHを自動でチェックするよ\n"
            "• 新商品は公開から10分以内のものだけ通知するよ\n"
            "• 何かおかしなときは自動でお知らせするよ\n"
            "• フィルターを設定すると、該当する商品だけが通知されるよ"
        ),
        inline=False,
    )
    embed.set_footer(text="BOOTH新作監視Bot • 快適なVRChatライフを！")

    try:
        await target_channel.send(embed=embed)
    except Exception as e:
        print(f"入室メッセージ送信エラー: {e}")


# ───────────────────────────────────────────
# カテゴリ選択 UI
# ───────────────────────────────────────────
class CategorySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(label="衣装", emoji="👗", description="3D衣装"),
            discord.SelectOption(label="髪", emoji="💇‍♀️", description="3D髪型"),
            discord.SelectOption(label="小物", emoji="💍", description="3D装飾品・小道具・靴"),
            discord.SelectOption(label="ギミック", emoji="⚡", description="3Dツール・システム"),
            discord.SelectOption(label="無料", emoji="🆓", description="無料アイテムのみ"),
        ]
        self.select = discord.ui.Select(
            placeholder="通知を受け取りたいジャンルを選択（複数OK）",
            min_values=1,
            max_values=5,
            options=options,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_categories = ",".join(self.select.values)
        channel_id = interaction.channel_id
        guild_id = interaction.guild_id

        async with db_connect() as db:
            await db.execute("""
                INSERT OR REPLACE INTO channels (channel_id, guild_id, categories)
                VALUES (?, ?, ?)
            """, (channel_id, guild_id, selected_categories))
            await db.commit()

        categories_display = ", ".join(self.select.values)
        await interaction.response.send_message(
            f"✅ このチャンネル（ID: {channel_id}）に **【{categories_display}】** の通知を設定したよ！",
            ephemeral=True,
        )


# ───────────────────────────────────────────
# スラッシュコマンド
# ───────────────────────────────────────────
@bot.tree.command(name="set-channel", description="このチャンネルにBOOTH新作通知を設定します")
@app_commands.checks.has_permissions(manage_channels=True)
async def set_channel(interaction: discord.Interaction):
    view = CategorySelectView()
    await interaction.response.send_message(
        "通知を受け取りたいジャンルを選んでね：", view=view, ephemeral=True
    )


@set_channel.error
async def set_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠️ このコマンドを実行するには **チャンネル管理** の権限が必要だよ。",
            ephemeral=True,
        )


@bot.tree.command(name="remove-channel", description="このチャンネルの通知設定を解除します")
@app_commands.checks.has_permissions(manage_channels=True)
async def remove_channel(interaction: discord.Interaction):
    async with db_connect() as db:
        await db.execute("DELETE FROM channels WHERE channel_id = ?", (interaction.channel_id,))
        await db.commit()
    await interaction.response.send_message(
        "❌ このチャンネルの通知設定を解除したよ。", ephemeral=True
    )


@bot.tree.command(name="filter", description="アバター名フィルターを管理します")
@app_commands.describe(action="実行する操作", name="アバター名")
async def filter_command(
    interaction: discord.Interaction,
    action: Literal["add", "remove", "list"],
    name: str = "",
):
    channel_id = interaction.channel_id

    if action == "list":
        async with db_connect() as db:
            filters = await load_channel_filters(db, channel_id)
        if not filters:
            await interaction.response.send_message(
                "📭 このチャンネルにはアバター名フィルターが登録されていないよ。",
                ephemeral=True,
            )
            return
        names = ", ".join([f"`{f[0]}`" for f in filters])
        await interaction.response.send_message(
            f"📌 このチャンネルに登録されたフィルター（{len(filters)}件）:\n{names}",
            ephemeral=True,
        )
        return

    # add / remove は名前必須
    if not name.strip():
        await interaction.response.send_message(
            "⚠️ アバター名を入力してね。例: `/filter add セレスティア`",
            ephemeral=True,
        )
        return

    normalized = normalize_avatar_name(name)
    if not normalized:
        await interaction.response.send_message(
            "⚠️ その名前ではフィルター登録できないよ。",
            ephemeral=True,
        )
        return

    async with db_connect() as db:
        if action == "add":
            try:
                await db.execute(
                    """
                    INSERT INTO filters (channel_id, avatar_name, normalized_name)
                    VALUES (?, ?, ?)
                    """,
                    (channel_id, name.strip(), normalized),
                )
                await db.commit()
                await interaction.response.send_message(
                    f"✅ 「`{name.strip()}`」をフィルターに追加したよ！\n"
                    f"（正規化: `{normalized}`）",
                    ephemeral=True,
                )
            except Exception as e:
                print(f"フィルター追加エラー: {e}")
                await interaction.response.send_message(
                    f"⚠️ 「`{name.strip()}`」は既に登録されているか、登録できないよ。",
                    ephemeral=True,
                )

        elif action == "remove":
            cursor = await db.execute(
                "DELETE FROM filters WHERE channel_id = ? AND normalized_name = ?",
                (channel_id, normalized),
            )
            await db.commit()
            if cursor.rowcount > 0:
                await interaction.response.send_message(
                    f"❌ 「`{name.strip()}`」をフィルターから削除したよ。",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ 「`{name.strip()}`」は登録されていないよ。",
                    ephemeral=True,
                )


@bot.tree.command(name="set-nsfw", description="このチャンネルでのR-18商品通知を切り替えます")
@app_commands.describe(mode="allow=表示 / deny=非表示")
async def set_nsfw(
    interaction: discord.Interaction,
    mode: Literal["allow", "deny"],
):
    channel_id = interaction.channel_id
    allow = 1 if mode == "allow" else 0

    async with db_connect() as db:
        # チャンネル登録が無くても設定できるように、INSERT OR REPLACE
        await db.execute(
            """
            INSERT INTO channels (channel_id, guild_id, categories, allow_nsfw)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET allow_nsfw = excluded.allow_nsfw
            """,
            (channel_id, interaction.guild_id or 0, "", allow),
        )
        await db.commit()

    status_text = "表示" if allow else "非表示"
    await interaction.response.send_message(
        f"🔞 このチャンネルでのR-18商品通知を **{status_text}** に設定したよ。",
        ephemeral=True,
    )


@bot.tree.command(name="test-notify", description="テスト用の新着通知を1件送信します")
@app_commands.checks.has_permissions(manage_channels=True)
async def test_notify(interaction: discord.Interaction):
    """テスト用の架空商品で通知メッセージを1件送信する。"""
    test_item = {
        "item_id": "99999999",
        "title": "【テスト】BOOTH通知Bot テスト用アバター衣装",
        "url": "https://booth.pm/ja/items/99999999",
        "price": "¥ 0",
        "likes": 42,
        "shop_name": "テストショップ",
        "shop_url": "https://booth.pm/ja/shops/00000000",
        "image_url": "https://booth.pximg.net/8f4cf23d-15cf-42f8-91e8-8c84bcdf8e39/i/8571315/full_size.jpg",
        "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "category_name": "衣装",
        "is_adult": False,
        "tags": json.dumps(["アバター衣装", "テスト", "VRChat"]),
    }

    target_channel_id = BOT_TEST_CHANNEL_ID or interaction.channel_id
    target_channel = bot.get_channel(target_channel_id)
    if not target_channel:
        try:
            target_channel = await bot.fetch_channel(target_channel_id)
        except Exception as e:
            await interaction.response.send_message(
                f"❌ 通知先チャンネルの取得に失敗しました: {e}", ephemeral=True
            )
            return

    async with db_connect() as db:
        # 通知テンプレートを流用するため、一時的にDBに対象チャンネルを登録
        await db.execute(
            """
            INSERT OR REPLACE INTO channels (channel_id, guild_id, categories, allow_nsfw)
            VALUES (?, ?, ?, ?)
            """,
            (
                target_channel_id,
                getattr(target_channel, "guild", None) and target_channel.guild.id,
                "衣装,無料",
                0,
            ),
        )
        await db.commit()

        await broadcast_item(test_item, db)

        # テスト用に登録したチャンネル設定を削除（元からあった場合は削除しない方がいいが、テストコマンドなので削除）
        await db.execute("DELETE FROM channels WHERE channel_id = ?", (target_channel_id,))
        await db.commit()

    await interaction.response.send_message(
        f"✅ テスト通知を <#{target_channel_id}> に送信しました。", ephemeral=True
    )


@test_notify.error
async def test_notify_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ このコマンドを使うには `チャンネル管理` 権限が必要です。", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ エラーが発生しました: {error}", ephemeral=True
        )


@bot.tree.command(name="status", description="このチャンネルの設定状態を確認します")
async def status(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    async with db_connect() as db:
        async with db.execute(
            "SELECT categories, allow_nsfw FROM channels WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()

        filters = await load_channel_filters(db, channel_id)

    if row is None:
        categories_text = "未設定"
        nsfw_text = "非表示（デフォルト）"
    else:
        categories_text = row[0] if row[0] else "未設定"
        nsfw_text = "表示" if row[1] else "非表示"

    filters_text = ", ".join([f"`{f[0]}`" for f in filters]) if filters else "未登録"

    embed = discord.Embed(
        title="📊 このチャンネルの設定",
        color=0xFF6473,
    )
    embed.add_field(name="通知カテゴリ", value=categories_text, inline=False)
    embed.add_field(name="R-18設定", value=nsfw_text, inline=False)
    embed.add_field(name="アバター名フィルター", value=filters_text, inline=False)
    embed.set_footer(text="BOOTH新作監視Bot")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ───────────────────────────────────────────
# BOOTH 取得層（Step 3 で実装）
# ───────────────────────────────────────────
async def fetch_with_retry(session: aiohttp.ClientSession, url: str) -> str | None:
    """HTTP GET をリトライ付きで実行する。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 200:
                    return await response.text()

                if response.status == 429:
                    retry_after = float(
                        response.headers.get("Retry-After", RETRY_BASE_DELAY * attempt)
                    )
                    print(
                        f"⚠️ [fetch] レートリミット (429) を受信: {url} "
                        f"→ {retry_after:.0f}秒後にリトライ ({attempt}/{MAX_RETRIES})"
                    )
                    await asyncio.sleep(retry_after)
                    continue

                print(
                    f"⚠️ [fetch] HTTP {response.status}: {url} "
                    f"(試行 {attempt}/{MAX_RETRIES})"
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"⚠️ [fetch] 通信エラー: {url} | {e} (試行 {attempt}/{MAX_RETRIES})")

        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    print(f"❌ [fetch] 最大リトライ回数に到達: {url}")
    return None


def parse_search_page(html: str) -> list[str]:
    """検索結果ページから商品IDのリストを抽出する。"""
    soup = bs4.BeautifulSoup(html, "html.parser")
    cards = soup.select("li.item-card") or soup.select(".item-card")

    if not cards:
        print("⚠️ [parse_search_page] アイテム要素が見つかりません — BOOTHのHTML構造が変わった可能性があります")
        return []

    item_ids: list[str] = []
    seen = set()
    for card in cards:
        link = card.select_one("a[href*='/items/']")
        if not link:
            continue
        href = link.get("href", "")
        match = re.search(r"/items/(\d+)", href)
        if not match:
            continue
        item_id = match.group(1)
        if item_id not in seen:
            seen.add(item_id)
            item_ids.append(item_id)

    return item_ids


async def fetch_item_json(session: aiohttp.ClientSession, item_id: str) -> dict | None:
    """商品JSON APIから詳細情報を取得する。"""
    url = f"https://booth.pm/ja/items/{item_id}.json"
    text = await fetch_with_retry(session, url)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"⚠️ [fetch_item_json] JSONデコード失敗 (ID: {item_id}): {e}")
        return None


def parse_item_json(item_id: str, data: dict, category_label: str) -> dict | None:
    """商品JSONをBot内部形式に変換する。"""
    title = data.get("name", "").strip()
    published_at = data.get("published_at", "").strip()

    if not title:
        print(f"⚠️ [parse_item_json] タイトルが無い商品をスキップ: ID={item_id}")
        return None
    if not published_at:
        print(f"⚠️ [parse_item_json] 公開日時が無い商品をスキップ: ID={item_id}")
        return None

    # 画像URL: 最初の画像の original を使用
    images = data.get("images", [])
    image_url = ""
    if images and isinstance(images, list):
        image_url = images[0].get("original", "") or ""

    # タグ
    tags = [tag.get("name", "") for tag in data.get("tags", []) if tag.get("name")]

    # BOOTH上のカテゴリ名
    category_name = ""
    category_data = data.get("category")
    if isinstance(category_data, dict):
        category_name = category_data.get("name", "") or ""

    return {
        "item_id": item_id,
        "title": title,
        "url": f"https://booth.pm/ja/items/{item_id}",
        "price": data.get("price", "不明"),
        "category": category_label,  # ユーザー向けラベル（例: "衣装"）
        "category_name": category_name,  # BOOTH上の生の名前（例: "3D衣装"）
        "likes": int(data.get("wish_lists_count", 0) or 0),
        "image_url": image_url,
        "is_adult": 1 if data.get("is_adult") else 0,
        "published_at": published_at,
        "shop_name": data.get("shop", {}).get("name", "") if isinstance(data.get("shop"), dict) else "",
        "tags": json.dumps(tags, ensure_ascii=False),
    }


# ───────────────────────────────────────────
# 通知ロジック（Step 4 で実装）
# ───────────────────────────────────────────
async def run_check_booth_job():
    """
    実際の巡回処理。
    成功したら True、失敗したら False を返す。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"\n🔍 --- 【巡回スタート】{now:%Y-%m-%d %H:%M:%S} ---")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. 検索ページから商品IDを収集
        all_item_ids: list[str] = []
        for page in range(1, SEARCH_PAGES + 1):
            url = SEARCH_URL_TEMPLATE.format(page=page)
            html = await fetch_with_retry(session, url)
            if html is None:
                continue
            ids = parse_search_page(html)
            print(f"📄 検索ページ {page}: {len(ids)} 件の商品IDを取得")
            for item_id in ids:
                if item_id not in all_item_ids:
                    all_item_ids.append(item_id)
            await asyncio.sleep(0.5)

        print(f"📦 重複除去後: {len(all_item_ids)} 件")

        # 検索結果がゼロなら失敗とみなす（BOOTH構造変更の可能性）
        if not all_item_ids:
            print("❌ 検索結果から商品IDが1件も取得できませんでした")
            return False

        # 2. 各商品のJSONを取得して処理
        async with db_connect() as db:
            new_items: list[dict] = []

            for item_id in all_item_ids:
                data = await fetch_item_json(session, item_id)
                if data is None:
                    continue

                # カテゴリ判定
                category_name = ""
                category_data = data.get("category")
                if isinstance(category_data, dict):
                    category_name = category_data.get("name", "") or ""

                category_label = map_category_label(category_name)
                if category_label is None and data.get("price") != "¥ 0":
                    # 無料でもなければスキップ
                    continue

                # 必須情報を取得
                published_at_str = data.get("published_at", "").strip()
                if not published_at_str:
                    continue

                try:
                    published_at = datetime.datetime.fromisoformat(published_at_str)
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue

                # 新作判定: LOOKBACK_MINUTES 以内に公開されたもの
                minutes_ago = (now - published_at).total_seconds() / 60
                is_new = 0 < minutes_ago <= LOOKBACK_MINUTES

                # 商品情報をパース
                item = parse_item_json(item_id, data, category_label or "")
                if item is None:
                    continue

                # DBに保存 or 更新
                await db.execute("""
                    INSERT INTO items (
                        item_id, title, url, price, category, likes, image_url,
                        is_adult, published_at, shop_name, tags, notified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        title = excluded.title,
                        url = excluded.url,
                        price = excluded.price,
                        category = excluded.category,
                        likes = excluded.likes,
                        image_url = excluded.image_url,
                        is_adult = excluded.is_adult,
                        published_at = excluded.published_at,
                        shop_name = excluded.shop_name,
                        tags = excluded.tags
                """, (
                    item["item_id"], item["title"], item["url"], item["price"],
                    item["category"], item["likes"], item["image_url"],
                    item["is_adult"], item["published_at"], item["shop_name"],
                    item["tags"], item.get("notified_at"),
                ))

                if is_new:
                    new_items.append(item)

                await asyncio.sleep(0.2)

            await db.commit()
            print(f"🆕 新作判定: {len(new_items)} 件")

            # 3. 通知処理
            for item in new_items:
                try:
                    await broadcast_item(item, db)
                    await db.execute(
                        "UPDATE items SET notified_at = ? WHERE item_id = ?",
                        (now, item["item_id"]),
                    )
                    await db.commit()
                except Exception as e:
                    print(f"❌ [check_booth_job] 通知エラー (ID: {item['item_id']}): {e}")

    print(f"🔍 --- 【巡回完了】{len(new_items)} 件通知 ---\n")
    return True


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_booth_job():
    """
    定期的にBOOTHを巡回して新作を通知する。
    失敗が続いたら自己申告で警告を出す。
    """
    async with db_connect() as db:
        failure_count = await get_failure_count(db)

        try:
            success = await run_check_booth_job()
        except Exception as e:
            print(f"❌ [check_booth_job] 巡回中に例外が発生: {e}")
            success = False

        if success:
            # 成功したら失敗カウントをリセット
            if failure_count != 0:
                await set_failure_count(db, 0)
                print("✅ 巡回に成功したので失敗カウントをリセット")
            return

        # 失敗したらカウントを増やす
        failure_count += 1
        await set_failure_count(db, failure_count)
        print(f"⚠️ 巡回失敗。連続失敗回数: {failure_count}/{FAILURE_ALERT_THRESHOLD}")

        # 3回連続失敗したら警告を出す（3回目だけ）
        if failure_count == FAILURE_ALERT_THRESHOLD:
            await send_admin_alert(
                title="🚨 BOOTH巡回が3回連続で失敗しました",
                description=(
                    f"BOOTHからの新作商品取得が **{FAILURE_ALERT_THRESHOLD}回連続** で失敗しています。\n"
                    "BOOTHのサイト構造が変わったか、通信に問題がある可能性があります。\n\n"
                    "確認すること:\n"
                    "- BOOTHの検索ページURLやHTML構造に変更がないか\n"
                    "- RenderのログにHTTPエラーや例外が出ていないか\n"
                    "- ネットワーク接続やレート制限の状態\n\n"
                    "ユーザーには一時的な不具合をお知らせ済みです。"
                ),
                color=0xFF0000,
            )
            await send_user_outage_notice(
                db,
                "⚠️ 現在、BOOTHからの情報取得に一時的な不具合が発生している可能性があります。"
                "復旧までしばらくお待ちください。\n"
                "（このメッセージは複数回表示されないことがあります）",
            )


async def load_channel_filters(db: aiosqlite.Connection, channel_id: int) -> list[tuple[str, str]]:
    """チャンネルに登録されたフィルターを返す。"""
    async with db.execute(
        "SELECT avatar_name, normalized_name FROM filters WHERE channel_id = ?",
        (channel_id,),
    ) as cursor:
        return await cursor.fetchall()


async def broadcast_item(item: dict, db: aiosqlite.Connection):
    """対象チャンネルにEmbedを送信する。"""
    item_labels = get_item_labels(item)
    if not item_labels:
        return

    async with db.execute("SELECT channel_id, guild_id, categories, allow_nsfw FROM channels") as cursor:
        channels = await cursor.fetchall()

    if not channels:
        return

    embed = discord.Embed(
        title=f"✨ 新着入荷！ [{', '.join(item_labels)}]",
        description=f"**[{item['title']}]({item['url']})**",
        color=0xFF6473,
        timestamp=datetime.datetime.fromisoformat(item["published_at"]),
    )
    embed.add_field(name="価格", value=item["price"], inline=True)
    embed.add_field(name="スキ数", value=f"❤️ {item['likes']}", inline=True)
    embed.add_field(name="ショップ", value=item["shop_name"] or "不明", inline=True)
    embed.set_footer(text="BOOTH新作監視Bot")

    if item["image_url"]:
        embed.set_image(url=item["image_url"])

    for channel_id, guild_id, categories_str, allow_nsfw in channels:
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                print(f"🗑️ 存在しないチャンネル (ID: {channel_id}) をDBから削除しました")
                await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
                await db.commit()
                continue
            except discord.Forbidden:
                print(f"⚠️ チャンネル (ID: {channel_id}) へのアクセス権限がありません")
                continue
            except Exception as e:
                print(f"❌ チャンネル取得失敗 (ID: {channel_id}): {e}")
                continue

        if not channel:
            continue

        # カテゴリ一致チェック
        subscribed_categories = [c.strip() for c in categories_str.split(",")]
        matched_label = None
        for label in item_labels:
            if label in subscribed_categories:
                matched_label = label
                break
        if matched_label is None:
            continue

        # R-18 チェック
        if item["is_adult"] and not allow_nsfw:
            continue

        # アバター名フィルターチェック
        filters = await load_channel_filters(db, channel_id)
        if filters:
            tag_names = json.loads(item["tags"]) if item["tags"] else []
            tag_names_normalized = [normalize_avatar_name(t) for t in tag_names]
            matched_filter = None
            for avatar_name, normalized_name in filters:
                # 部分一致: タグの中にフィルター文字列が含まれるか
                if any(normalized_name in tag_norm for tag_norm in tag_names_normalized):
                    matched_filter = avatar_name
                    break
            if not matched_filter:
                continue
            embed.description = (
                f"**[{item['title']}]({item['url']})**\n"
                f"🏷️ マッチしたフィルター: `{matched_filter}`"
            )
        else:
            # フィルターが無い場合は元のdescriptionに戻す
            embed.description = f"**[{item['title']}]({item['url']})**"

        try:
            await channel.send(embed=embed)
            print(f"🚀 【送信成功】#{channel.name} に 「{item['title'][:20]}...」 を通知")
            await asyncio.sleep(0.3)
        except discord.Forbidden:
            print(f"❌ 【送信失敗】#{channel.name} への送信権限がありません（次回巡回で再試行します）")
        except discord.HTTPException as e:
            print(f"❌ 【送信HTTPエラー】#{channel.name}: {e}（次回巡回で再試行します）")
        except Exception as e:
            print(f"❌ 【送信エラー】#{channel.name}: {e}（次回巡回で再試行します）")


# ───────────────────────────────────────────
# 起動イベント
# ───────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"🎉 {bot.user.name} が正常に起動しました")


async def main():
    await start_web_server()
    if not DISCORD_TOKEN:
        print("⚠️ エラー: DISCORD_BOT_TOKEN が設定されていません")
        return
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
