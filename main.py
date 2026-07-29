import asyncio
import datetime
import os
import re
import aiohttp
from aiohttp import web
import aiosqlite
import bs4
import discord
from discord import app_commands
from discord.ext import commands, tasks

# Renderの環境変数（DISCORD_BOT_TOKEN）から安全にトークンを取得します
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# BOOTH公式サブカテゴリのURLマッピング
CATEGORY_URLS = {
    "衣装": "https://booth.pm/ja/search/VRChat?category=3d_clothing&sort=new",
    "髪": "https://booth.pm/ja/search/VRChat?category=3d_hair&sort=new",
    "小物": "https://booth.pm/ja/search/VRChat?category=3d_accessory&sort=new",
    "ギミック": (
        "https://booth.pm/ja/search/VRChat?category=3d_tool_system&sort=new"
    ),
}

intents = discord.Intents.default()


class MyBot(commands.Bot):

    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.init_db()
        await self.tree.sync()
        print("✅ スラッシュコマンドの同期が完了しました！")
        check_booth_job.start()

    async def init_db(self):
        async with aiosqlite.connect("bot_data.db") as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    categories TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tracked_items (
                    item_id TEXT PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    price TEXT,
                    category TEXT,
                    likes INTEGER,
                    created_at TIMESTAMP,
                    notified INTEGER DEFAULT 0
                )
            """)
            await db.commit()


bot = MyBot()


# --- Botがサーバーに参加した時の自動挨拶＆説明機能 ---
@bot.event
async def on_guild_join(guild: discord.Guild):
    target_channel = guild.system_channel

    if (
        target_channel is None
        or not target_channel.permissions_for(guild.me).send_messages
    ):
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break

    if target_channel is None:
        return

    embed = discord.Embed(
        title="🎉 BOOTH通知Bot が導入されました！",
        description=(
            "導入ありがとうございます！\n"
            "このBotは、BOOTHのVRChat向け新作アイテム（衣装・髪・小物・ギミック）を監視し、"
            "**スキ（❤️）が 300 を超えた人気商品** を自動でお知らせします！"
        ),
        color=0xFF6473,
    )

    embed.add_field(
        name="📌 基本コマンド（使い方）",
        value=(
            "`/set-channel`\n"
            "➔ このコマンドを実行したチャンネルに通知を設定します。（ジャンル選択可）\n\n"
            "`/remove-channel`\n"
            "➔ このチャンネルの通知設定を解除します。"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚙️ 初期設定の手順",
        value=(
            "1. 通知を受け取りたいテキストチャンネルに移動します。\n2."
            " `/set-channel`"
            " と入力して送信し、ドロップダウンから通知したいジャンルを選んでください！"
        ),
        inline=False,
    )

    embed.set_footer(text="BOOTH新作監視Bot • 快適なVRChatライフを！")

    try:
        await target_channel.send(embed=embed)
    except Exception as e:
        print(f"入室メッセージ送信エラー: {e}")


# --- Webサーバー機能 ---
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
    print(f"🌐 Webサーバーがポート {port} で起動しました！")


# --- ドロップダウンUI ---
class CategorySelectView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)

        options = [
            discord.SelectOption(
                label="衣装", emoji="👗", description="3D衣装"
            ),
            discord.SelectOption(
                label="髪", emoji="💇‍♀️", description="3D髪型"
            ),
            discord.SelectOption(
                label="小物",
                emoji="💍",
                description="3D装飾品・小道具・靴",
            ),
            discord.SelectOption(
                label="ギミック",
                emoji="⚡",
                description="3Dツール・システム",
            ),
        ]

        self.select = discord.ui.Select(
            placeholder="通知を受け取りたいジャンルを選択（複数OK）",
            min_values=1,
            max_values=4,
            options=options,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_categories = ",".join(self.select.values)
        channel_id = interaction.channel_id
        guild_id = interaction.guild_id

        async with aiosqlite.connect("bot_data.db") as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO channels (channel_id, guild_id, categories)
                VALUES (?, ?, ?)
            """,
                (channel_id, guild_id, selected_categories),
            )
            await db.commit()

        categories_display = ", ".join(self.select.values)
        await interaction.response.send_message(
            f"✅ このチャンネル（ID: {channel_id}）に"
            f" **【{categories_display}】** の通知を設定したよ！",
            ephemeral=True,
        )
        print(
            f"📌 【チャンネル登録完了】Channel ID: {channel_id} に"
            f" {categories_display} を保存しました"
        )


@bot.tree.command(
    name="set-channel",
    description="このチャンネルにBOOTH新作通知を設定します",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def set_channel(interaction: discord.Interaction):
    view = CategorySelectView()
    await interaction.response.send_message(
        "通知を受け取りたいジャンルを選んでね：", view=view, ephemeral=True
    )


@set_channel.error
async def set_channel_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠️ このコマンドを使うには「チャンネルの管理」権限が必要です！",
            ephemeral=True,
        )


@bot.tree.command(
    name="remove-channel",
    description="このチャンネルの通知設定を解除します",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def remove_channel(interaction: discord.Interaction):
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute(
            "DELETE FROM channels WHERE channel_id = ?",
            (interaction.channel_id,),
        )
        await db.commit()
    await interaction.response.send_message(
        "❌ このチャンネルの通知設定を解除したよ。", ephemeral=True
    )


# --- 1分ごとのBOOTH巡回（構造解析強化版） ---
@tasks.loop(minutes=1)
async def check_booth_job():
    print("\n🔍 --- 【1分巡回スタート】BOOTHのチェックを開始します ---")

    async with aiosqlite.connect("bot_data.db") as db:
        async with db.execute("SELECT COUNT(*) FROM channels") as cursor:
            channel_count = (await cursor.fetchone())[0]

    print(
        f"📊 現在データベースに登録されている通知先チャンネル数: {channel_count}"
        " 件"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        for cat_name, url in CATEGORY_URLS.items():
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as response:
                    if response.status != 200:
                        print(
                            f"⚠️ [{cat_name}] アクセス失敗（ステータスコード:"
                            f" {response.status}）"
                        )
                        continue
                    html = await response.text()

                soup = bs4.BeautifulSoup(html, "html.parser")

                # BOOTHの要素検索を複数パターンで試行
                items = (
                    soup.select("li.item-card")
                    or soup.select(".item-card")
                    or soup.select("ul.grid > li")
                )
                print(
                    f"📦 [{cat_name}] BOOTHから取得できたアイテム数: {len(items)}"
                    " 件"
                )

                parsed_count = 0
                async with aiosqlite.connect("bot_data.db") as db:
                    for item in items:
                        try:
                            # タイトル・URLの取得（複数パターン対応）
                            url_tag = (
                                item.select_one("a.item-card__title-anchor")
                                or item.select_one(".item-card__title a")
                                or item.select_one("a[href*='/items/']")
                            )

                            if not url_tag:
                                continue

                            item_url = url_tag.get("href", "")
                            if not item_url.startswith("http"):
                                item_url = "https://booth.pm" + item_url

                            item_id = item_url.split("/")[-1].split("?")[0]
                            title = url_tag.text.strip() or "タイトル不明"

                            # 価格の取得
                            price_tag = item.select_one(
                                ".price"
                            ) or item.select_one(".item-card__price")
                            price = (
                                price_tag.text.strip() if price_tag else "不明"
                            )

                            # スキ数の取得
                            like_tag = item.select_one(
                                ".js-like-count"
                            ) or item.select_one(".item-card__like-count")
                            likes = 0
                            if like_tag:
                                like_text = re.sub(r"[^\d]", "", like_tag.text)
                                likes = int(like_text) if like_text else 0

                            parsed_count += 1

                            # DB保存
                            now = datetime.datetime.now()
                            await db.execute(
                                """
                                INSERT OR IGNORE INTO tracked_items (item_id, title, url, price, category, likes, created_at, notified)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                            """,
                                (
                                    item_id,
                                    title,
                                    item_url,
                                    price,
                                    cat_name,
                                    likes,
                                    now,
                                ),
                            )
                            await db.commit()

                            # 通知送信処理へ
                            await broadcast_item(
                                item_id,
                                title,
                                item_url,
                                price,
                                cat_name,
                                likes,
                                db,
                            )

                        except Exception as e:
                            print(f"⚠️ アイテム個別解析エラー: {e}")
                            continue

                print(
                    f"✅ [{cat_name}] 解析・送信成功数: {parsed_count} /"
                    f" {len(items)} 件"
                )

            except Exception as e:
                print(f"❌ [{cat_name}] 巡回通信エラー: {e}")


async def broadcast_item(item_id, title, url, price, category, likes, db):
    async with db.execute(
        "SELECT channel_id, categories FROM channels"
    ) as cursor:
        channels = await cursor.fetchall()

    if not channels:
        return

    embed = discord.Embed(
        title=f"❤️ スキ達成！ [{category}]（※テスト強制通知）",
        description=f"**[{title}]({url})**",
        color=0xFF6473,
    )
    embed.add_field(name="価格", value=price, inline=True)
    embed.add_field(name="スキ数", value=f"❤️ {likes}", inline=True)
    embed.set_footer(text="BOOTH新作監視Bot")

    for channel_id, categories_str in channels:
        cat_list = [c.strip() for c in categories_str.split(",")]
        if category in cat_list:
            channel = bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception as e:
                    print(
                        f"❌ 【チャンネル取得失敗】ID {channel_id}: {e}"
                    )
                    continue

            if channel:
                try:
                    await channel.send(embed=embed)
                    print(
                        f"🚀 【送信成功】#{channel.name} に 「{title[:15]}...」"
                        " を通知しました！"
                    )
                    await asyncio.sleep(0.2)
                except discord.Forbidden:
                    print(
                        f"❌ 【送信失敗】#{channel.name}"
                        " への送信権限がありません（ボット権限確認）"
                    )
                except Exception as e:
                    print(f"❌ 【送信エラー】#{channel.name}: {e}")


@bot.event
async def on_ready():
    print(f"🎉 {bot.user.name} が正常に起動しました！")


async def main():
    await start_web_server()
    if not DISCORD_TOKEN:
        print("⚠️ エラー: DISCORD_BOT_TOKEN が設定されていません！")
        return
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
