"""
BOOTH VRChat 新作通知 Bot
- BOOTH JSON API を使用
- published_at で新作判定
- チャンネルごとにカテゴリ / アバターフィルター（BOOTH商品ID） / R-18 設定
"""

import asyncio
import datetime
import json
import os
import sys
import traceback
from typing import Literal

import aiohttp
from aiohttp import web
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    clear_legacy_name_filters,
    count_legacy_name_filters,
    db_connect,
    get_failure_count,
    init_db,
    load_all_channel_filters,
    load_channel_filters,
    load_channel_shop_filters,
    set_failure_count,
    validate_db_connection,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from utils import (
    MATCH_BY_ID,
    MAX_FILTERS_PER_CHANNEL,
    MAX_FILTER_NAME_LENGTH,
    clean_avatar_display_name,
    extract_booth_item_id,
    extract_name_aliases,
    match_avatar_filters,
    normalize_avatar_name,
    validate_filter_name,
)
import logging_utils as log
from booth import (
    AVATAR_CATEGORY_NAMES,
    CATEGORY_LABELS as BOOTH_CATEGORY_LABELS,
    CHECK_INTERVAL_MINUTES,
    FAILURE_ALERT_THRESHOLD,
    LOOKBACK_MINUTES,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    SEARCH_PAGES,
    SEARCH_URL_TEMPLATE,
    get_item_labels,
    map_category_label,
    parse_item_json,
    parse_search_page,
)

# ───────────────────────────────────────────
# 定数
# ───────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = "bot_data.db"
ADMIN_CHANNEL_ID_RAW = os.getenv("ADMIN_CHANNEL_ID", "")
ADMIN_CHANNEL_ID = int(ADMIN_CHANNEL_ID_RAW) if ADMIN_CHANNEL_ID_RAW.strip().lstrip("-").isdigit() else None

BOT_TEST_CHANNEL_ID_RAW = os.getenv("BOT_TEST_CHANNEL_ID", "")
BOT_TEST_CHANNEL_ID = int(BOT_TEST_CHANNEL_ID_RAW) if BOT_TEST_CHANNEL_ID_RAW.strip().lstrip("-").isdigit() else None

MANAGER_USER_ID_RAW = os.getenv("MANAGER_USER_ID", "")
MANAGER_USER_ID = int(MANAGER_USER_ID_RAW) if MANAGER_USER_ID_RAW.strip().lstrip("-").isdigit() else None

CATEGORY_LABELS = BOOTH_CATEGORY_LABELS

# BOOTHへのリクエスト共通ヘッダ
BOOTH_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def validate_environment() -> list[str]:
    """必須・推奨環境変数をチェックし、問題のリストを返す。"""
    issues: list[str] = []
    if not DISCORD_TOKEN:
        issues.append("DISCORD_BOT_TOKEN が未設定")
    if ADMIN_CHANNEL_ID is None:
        issues.append("ADMIN_CHANNEL_ID が未設定または無効")
    if MANAGER_USER_ID is None:
        issues.append("MANAGER_USER_ID が未設定または無効")
    if not TURSO_DATABASE_URL:
        issues.append("TURSO_DATABASE_URL が未設定（Renderではデータが揮発する可能性があります）")
    if not TURSO_AUTH_TOKEN:
        issues.append("TURSO_AUTH_TOKEN が未設定（Renderではデータが揮発する可能性があります）")
    return issues


intents = discord.Intents.default()
intents.message_content = True
intents.members = True


# ───────────────────────────────────────────
# Bot 本体
# ───────────────────────────────────────────
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        async with db_connect() as db:
            await init_db(db)
        await self.tree.sync()
        log.info("setup_hook", "✅ スラッシュコマンドの同期が完了しました")
        check_booth_job.start()
        send_dm_replies.start()

# 管理用チャンネルに詳細警告を送る（未設定/失敗時は Manager に DM フォールバック）
async def send_admin_alert(title: str, description: str, color: int = 0xFF0000) -> None:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text="BOOTH通知Bot 自己申告システム")

    channel_send_error = None
    if ADMIN_CHANNEL_ID is not None:
        log.info("send_admin_alert", f"📤 管理用警告送信開始: {title} → ADMIN_CHANNEL_ID={ADMIN_CHANNEL_ID}")
        channel = bot.get_channel(ADMIN_CHANNEL_ID)
        fetch_error = None
        if channel is None:
            try:
                channel = await bot.fetch_channel(ADMIN_CHANNEL_ID)
                log.info("send_admin_alert", f"✅ 管理用チャンネルを fetch_channel で取得: #{getattr(channel, 'name', 'N/A')}")
            except Exception as e:
                fetch_error = str(e)
                log.error("send_admin_alert", f"❌ 管理用チャンネル取得失敗 (ID: {ADMIN_CHANNEL_ID}): {e}")

        if channel is not None:
            try:
                await channel.send(embed=embed)
                log.info("send_admin_alert", f"🚨 管理用チャンネルに警告を送信: {title}")
                return
            except Exception as e:
                channel_send_error = str(e)
                log.error("send_admin_alert", f"❌ 管理用チャンネルへの警告送信失敗: {e}")
        else:
            channel_send_error = fetch_error or "チャンネルがNoneです"
    else:
        log.info("send_admin_alert", f"📤 ADMIN_CHANNEL_ID 未設定のため Manager DM を試行: {title}")

    # チャンネル未設定 or 送信失敗時は Manager に DM フォールバック
    if MANAGER_USER_ID is not None:
        try:
            manager = await bot.fetch_user(MANAGER_USER_ID)
            if manager is not None:
                if ADMIN_CHANNEL_ID is not None and channel_send_error:
                    fallback_desc = (
                        f"{description}\n\n"
                        f"⚠️ 元の管理用チャンネル (ID: {ADMIN_CHANNEL_ID}) への送信に失敗しました:\n"
                        f"`{channel_send_error}`"
                    )
                else:
                    fallback_desc = (
                        f"{description}\n\n"
                        "⚠️ ADMIN_CHANNEL_ID が未設定なので、Manager DM にフォールバックしています。"
                    )
                fallback_embed = discord.Embed(
                    title=f"【フォールバック】{title}",
                    description=fallback_desc,
                    color=color,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                fallback_embed.set_footer(text="BOOTH通知Bot 自己申告システム")
                await manager.send(embed=fallback_embed)
                log.info("send_admin_alert", f"📩 Managerユーザー (ID: {MANAGER_USER_ID}) に警告をDM送信: {title}")
        except Exception as e:
            log.error("send_admin_alert", f"❌ ManagerユーザーへのDM送信も失敗 (ID: {MANAGER_USER_ID}): {e}")
    else:
        log.warn("send_admin_alert", "⚠️ ADMIN_CHANNEL_ID も MANAGER_USER_ID も未設定: アラートを送信できません")


# 登録済み全チャンネルにユーザー向け告知を送る
async def send_user_outage_notice(db: aiosqlite.Connection, message_text: str) -> None:
    async with db.execute("SELECT channel_id FROM channels") as cursor:
        rows = await cursor.fetchall()

    if not rows:
        log.info("send_user_outage_notice", "ℹ️ 通知設定されているチャンネルがないのでユーザー告知をスキップ")
        return

    for (channel_id,) in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                log.info("send_user_outage_notice", f"🗑️ 存在しないチャンネル (ID: {channel_id}) をDBから削除")
                await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
                await db.commit()
                continue
            except Exception as e:
                log.error("send_user_outage_notice", f"❌ チャンネル取得失敗 (ID: {channel_id}): {e}")
                continue

        if channel is None:
            continue

        try:
            await channel.send(message_text)
            log.info("send_user_outage_notice", f"📢 ユーザー告知送信: #{channel.name}")
        except discord.Forbidden:
            log.error("send_user_outage_notice", f"❌ ユーザー告知送信権限なし: #{channel.name} (ID: {channel_id})")
        except Exception as e:
            log.error("send_user_outage_notice", f"❌ ユーザー告知送信失敗: #{channel.name}: {e}")


bot = MyBot()


# ───────────────────────────────────────────
# グローバルエラーハンドラ
# ───────────────────────────────────────────
@bot.event
async def on_error(event: str, *args, **kwargs):
    """未捕捉のイベントエラーをManagerに通知する。"""
    exc_info = sys.exc_info()
    error_text = traceback.format_exception(*exc_info) if exc_info[0] else ["不明なエラー"]
    error_summary = error_text[-1].strip() if error_text else "不明なエラー"
    full_traceback = "".join(error_text)

    log.error("on_error", f"❌ [on_error] イベント '{event}' で未捕捉例外:")
    print(full_traceback)

    await send_admin_alert(
        title=f"🚨 未捕捉例外: {event}",
        description=(
            f"イベント `{event}` で例外が発生しました。\n\n"
            f"```\n{error_summary[:500]}\n```\n\n"
            f"詳細はRenderのログを確認してください。"
        ),
        color=0xFF0000,
    )


# ───────────────────────────────────────────
# DM ブリッジ（ユーザー ↔ BoothBOT Manager）
# ───────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    """BotへのDMをManager側のログに転送し、返信可能にする。"""
    # 自分自身や他のBotは無視
    if message.author.bot:
        return
    # ギルド（サーバー）内のメッセージは無視
    if message.guild is not None:
        return
    # BotへのDMだけ処理
    if message.author.id == bot.user.id:
        return

    user = message.author
    content = message.content or ""
    attachments = message.attachments
    attachment_urls = [a.url for a in attachments] if attachments else []
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Manager側のターミナル/ログに出力（目立つように区切り付き）
    print("\n" + "=" * 60)
    log.info("on_message", f"📩 DM受信 from {user.display_name} ({user.name} / ID: {user.id})")
    log.info("on_message", f"📝 {content}")
    if attachment_urls:
        log.info("on_message", f"📎 添付: {', '.join(attachment_urls)}")
    log.info("on_message", f"💡 返信する: /reply user:{user.id} message:ここに返信内容")
    print("=" * 60 + "\n")

    # DBに保存（外からも確認できるように）
    try:
        async with db_connect() as db:
            await db.execute("""
                INSERT INTO dm_inbox (user_id, username, display_name, content, attachments, created_at, replied)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (
                user.id,
                str(user.name),
                str(user.display_name),
                content,
                json.dumps(attachment_urls, ensure_ascii=False),
                created_at,
            ))
            await db.commit()
    except Exception as e:
        log.warn("on_message", f"⚠️ DM保存失敗: {e}")

    # ユーザーに転送完了を返信
    try:
        await message.channel.send(
            "✅ メッセージをBoothBOT Managerに転送しました。\n"
            "　追って返信が届くので少々お待ちください。"
        )
    except Exception as e:
        log.warn("on_message", f"⚠️ DM転送確認メッセージ送信失敗: {e}")


@bot.tree.command(name="reply", description="DMブリッジ：指定ユーザーにBotから返信を送信する（Manager用）")
@app_commands.describe(
    user_id="返信先のDiscordユーザーID",
    message="送信するメッセージ",
)
async def reply_command(interaction: discord.Interaction, user_id: str, message: str):
    """Manager側が /reply コマンドでユーザーにDM返信する。"""
    await interaction.response.defer(ephemeral=True)

    # Manager権限チェック
    if MANAGER_USER_ID is None:
        await interaction.followup.send(
            "❌ MANAGER_USER_ID が設定されていないので /reply は使用できません。", ephemeral=True
        )
        return
    if interaction.user.id != MANAGER_USER_ID:
        await interaction.followup.send(
            "❌ このコマンドは BoothBOT Manager のみ使用できます。", ephemeral=True
        )
        return

    # user_id を数値に変換
    try:
        target_id = int(user_id.strip())
    except ValueError:
        await interaction.followup.send("❌ user_id は数字で入力してください。", ephemeral=True)
        return

    try:
        target_user = await bot.fetch_user(target_id)
        if target_user is None:
            await interaction.followup.send("❌ 指定したユーザーが見つかりません。", ephemeral=True)
            return

        await target_user.send(message)
        await interaction.followup.send(
            f"✅ {target_user.display_name} ({target_user.id}) に返信を送信しました。\n\n{message}",
            ephemeral=True,
        )
        log.info("reply_command", f"📤 Managerから返信送信 to {target_user.display_name} ({target_user.id}): {message}")
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ そのユーザーにはDMを送信できません。DMを受け取れない設定か、ブロックされています。",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 送信エラー: {e}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 予期しないエラー: {e}", ephemeral=True)


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
    log.info("start_web_server", f"🌐 Webサーバーがポート {port} で起動しました")


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
        description=(
            "導入ありがとうございます！\n"
            "BOOTHのVRChat向け新作アイテムを自動でお知らせするBotです。\n\n"
            "**まずは下の3ステップだけやればOK！**"
        ),
        color=0xFF6473,
    )
    embed.add_field(
        name="1️⃣ 通知チャンネルを決める（必須）",
        value=(
            "通知を受け取りたいチャンネルで `/set-channel` を実行してね。\n"
            "ジャンル（衣装 / 髪 / 小物 / ギミック / 無料）を選べるよ。\n"
            "**ここまでやれば通知が届き始めるよ。**"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ 通知を絞り込む（任意）",
        value=(
            "特定のアバター向けだけ受け取りたいときは `/filter` を使ってね。\n"
            "アバターは **BOOTHの商品URL（末尾の7桁ID）** で登録するよ。\n"
            "例: `https://booth.pm/ja/items/6106863`\n"
            "通知には**アバター名**で表示されるよ。\n"
            "**登録しなければ、選んだジャンルの新作は全部通知されるよ。**"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ R-18の表示を決める（任意）",
        value=(
            "初期状態ではR-18商品は**非表示**だよ。\n"
            "表示したいときは `/set-nsfw allow` を実行してね。"
        ),
        inline=False,
    )
    embed.add_field(
        name="📖 もっと詳しく",
        value="`/help` でいつでも使い方を確認できるよ。`/status` で今の設定が見れるよ。",
        inline=False,
    )
    embed.add_field(
        name="📝 このBotの動き",
        value=(
            "• 5分に1回BOOTHを自動でチェックするよ\n"
            "• 公開から10分以内の新作だけ通知するよ\n"
            "• 障害が起きたときは自動でお知らせするよ"
        ),
        inline=False,
    )
    embed.set_footer(text="BOOTH通知Bot（非公式） / BOOTHの公式Botではありません")

    try:
        await target_channel.send(embed=embed)
    except Exception as e:
        log.info("on_guild_join", f"入室メッセージ送信エラー: {e}")


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


class AvatarFilterModal(discord.ui.Modal):
    """アバターフィルター用モーダル（BOOTHのURL / 商品IDで指定）。"""

    value = discord.ui.TextInput(
        label="アバターのBOOTH URL または 商品ID",
        placeholder="https://booth.pm/ja/items/1234567  または  1234567",
        required=True,
        max_length=200,
    )

    def __init__(self, action: Literal["add", "remove"]):
        self.action = action
        action_label = "追加" if action == "add" else "削除"
        super().__init__(title=f"アバターフィルターの{action_label}")

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        raw_value = str(self.value).strip()

        item_id, error = extract_booth_item_id(raw_value)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        # 削除は DB だけ見れば済む
        if self.action == "remove":
            async with db_connect() as db:
                cursor = await db.execute(
                    "DELETE FROM avatar_filters WHERE channel_id = ? AND avatar_item_id = ?",
                    (channel_id, item_id),
                )
                await db.commit()
            if (cursor.rowcount or 0) > 0:
                await interaction.response.send_message(
                    f"❌ アバターフィルター（ID: `{item_id}`）を削除したよ。", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ ID `{item_id}` は登録されていないよ。`/filter` で一覧を確認してね。",
                    ephemeral=True,
                )
            return

        # 追加は BOOTH に問い合わせて名前を取ってくる
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with db_connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM avatar_filters WHERE channel_id = ?",
                (channel_id,),
            ) as cur:
                current_count = (await cur.fetchone())[0]

            if current_count >= MAX_FILTERS_PER_CHANNEL:
                await interaction.followup.send(
                    f"⚠️ このチャンネルのアバターフィルターは上限"
                    f"（{MAX_FILTERS_PER_CHANNEL}件）に達しているよ。\n"
                    "`/filter` からいくつか削除してから追加してね。",
                    ephemeral=True,
                )
                return

            data = await fetch_booth_item(item_id)
            if data is None:
                await interaction.followup.send(
                    f"⚠️ BOOTHで ID `{item_id}` の商品が見つからなかったよ。\n"
                    "アバターの商品ページのURLをそのまま貼ってみてね。",
                    ephemeral=True,
                )
                return

            title = (data.get("name") or "").strip()
            category_name = ""
            category_data = data.get("category")
            if isinstance(category_data, dict):
                category_name = category_data.get("name", "") or ""

            display_name = clean_avatar_display_name(title)
            normalized = normalize_avatar_name(display_name)
            aliases = extract_name_aliases(title)
            item_url = f"https://booth.pm/ja/items/{item_id}"

            try:
                await db.execute(
                    """
                    INSERT INTO avatar_filters
                        (channel_id, avatar_item_id, avatar_name, normalized_name, aliases, item_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_id,
                        item_id,
                        display_name,
                        normalized,
                        json.dumps(aliases, ensure_ascii=False),
                        item_url,
                    ),
                )
                await db.commit()
            except Exception as e:
                log.warn("AvatarFilterModal", f"⚠️ アバターフィルター追加エラー: {e}")
                await interaction.followup.send(
                    f"⚠️ アバター「`{display_name}`」（ID: `{item_id}`）は既に登録されているみたい。",
                    ephemeral=True,
                )
                return

        lines = [
            f"✅ アバター **{display_name}** をフィルターに追加したよ！",
            f"　🆔 ID: `{item_id}`　（{current_count + 1}/{MAX_FILTERS_PER_CHANNEL}件）",
            f"　📦 商品名: {title[:80]}",
            f"　🔗 {item_url}",
        ]
        if category_name and category_name not in AVATAR_CATEGORY_NAMES:
            lines.append(
                f"\n⚠️ このIDのカテゴリは「{category_name}」でアバター本体じゃないみたい。"
                "アバター本体のページのURLか確認してね（登録自体はできてるよ）。"
            )
        lines.append(
            "\n💡 商品説明に **このアバターのURLが貼られている新作** を通知するよ。"
            "URLが無くても名前が一致したら拾うようにしてるよ。"
        )
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class ShopFilterModal(discord.ui.Modal):
    """ショップ名フィルター用モーダル。"""

    name = discord.ui.TextInput(
        label="ショップ名",
        placeholder="例: ポンデロニウム研究所",
        required=True,
        max_length=MAX_FILTER_NAME_LENGTH,
    )

    def __init__(self, action: Literal["add", "remove"]):
        self.action = action
        action_label = "追加" if action == "add" else "削除"
        super().__init__(title=f"ショップ名フィルターの{action_label}")

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        name_value = str(self.name).strip()

        normalized, error = validate_filter_name(name_value)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        async with db_connect() as db:
            if self.action == "add":
                async with db.execute(
                    "SELECT COUNT(*) FROM shop_filters WHERE channel_id = ?",
                    (channel_id,),
                ) as cur:
                    current_count = (await cur.fetchone())[0]
                if current_count >= MAX_FILTERS_PER_CHANNEL:
                    await interaction.response.send_message(
                        f"⚠️ このチャンネルのショップ名フィルターは上限"
                        f"（{MAX_FILTERS_PER_CHANNEL}件）に達しているよ。\n"
                        "`/filter` からいくつか削除してから追加してね。",
                        ephemeral=True,
                    )
                    return

                try:
                    await db.execute(
                        """
                        INSERT INTO shop_filters (channel_id, shop_name, normalized_name)
                        VALUES (?, ?, ?)
                        """,
                        (channel_id, name_value, normalized),
                    )
                    await db.commit()
                    await interaction.response.send_message(
                        f"✅ ショップ名「`{name_value}`」をフィルターに追加したよ！\n"
                        f"（正規化: `{normalized}` / {current_count + 1}/{MAX_FILTERS_PER_CHANNEL}件）",
                        ephemeral=True,
                    )
                except Exception as e:
                    log.warn("ShopFilterModal", f"⚠️ フィルター追加エラー: {e}")
                    await interaction.response.send_message(
                        f"⚠️ ショップ名「`{name_value}`」は既に登録されているか、登録できないよ。",
                        ephemeral=True,
                    )
            else:
                cursor = await db.execute(
                    "DELETE FROM shop_filters WHERE channel_id = ? AND normalized_name = ?",
                    (channel_id, normalized),
                )
                await db.commit()
                if (cursor.rowcount or 0) > 0:
                    await interaction.response.send_message(
                        f"❌ ショップ名「`{name_value}`」をフィルターから削除したよ。",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"⚠️ ショップ名「`{name_value}`」は登録されていないよ。",
                        ephemeral=True,
                    )


class FilterDeleteButton(discord.ui.Button):
    """登録済みフィルターを削除するボタン。"""

    def __init__(self, target: Literal["avatar", "shop"], key: str, display_name: str, row: int = 0):
        self.target = target
        self.key = key
        self.display_name = display_name
        super().__init__(
            label=f"❌ {display_name[:20]}",
            style=discord.ButtonStyle.danger,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id

        async with db_connect() as db:
            if self.target == "avatar":
                cursor = await db.execute(
                    "DELETE FROM avatar_filters WHERE channel_id = ? AND avatar_item_id = ?",
                    (channel_id, self.key),
                )
                target_label = "アバター"
            else:
                cursor = await db.execute(
                    "DELETE FROM shop_filters WHERE channel_id = ? AND normalized_name = ?",
                    (channel_id, self.key),
                )
                target_label = "ショップ名"
            await db.commit()

        if (cursor.rowcount or 0) > 0:
            await interaction.response.send_message(
                f"❌ {target_label}「`{self.display_name}`」を削除したよ。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ 既に削除されているよ。", ephemeral=True
            )


class FilterListView(discord.ui.View):
    """登録済みフィルターをボタン付きで表示するビュー。追加ボタンも含む。

    Discordの制約（1行5個・最大5行）に合わせて、アバターは row0-1、
    ショップは row2-3、追加ボタンは row4 に置く。
    """

    def __init__(self, avatar_filters: list[tuple], shop_filters: list[tuple[str, str]]):
        super().__init__(timeout=180)
        for i, entry in enumerate(avatar_filters[:10]):
            self.add_item(FilterDeleteButton("avatar", str(entry[0]), entry[1], row=i // 5))
        for i, (display_name, normalized) in enumerate(shop_filters[:10]):
            self.add_item(FilterDeleteButton("shop", normalized, display_name, row=2 + i // 5))
        self.add_item(FilterAddMenuButton())


class FilterAddMenuButton(discord.ui.Button):
    """フィルター追加用の選択画面を開くボタン。"""

    def __init__(self):
        super().__init__(label="➕ フィルターを追加", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="追加するフィルターの種類を選んでね。",
            view=FilterTargetSelect("add"),
        )


class FilterActionSelect(discord.ui.View):
    """フィルターが未登録の時に表示する追加ボタンのみのビュー。"""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="➕ フィルターを追加", style=discord.ButtonStyle.primary)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="追加するフィルターの種類を選んでね。",
            view=FilterTargetSelect("add"),
        )


class FilterTargetSelect(discord.ui.View):
    """アバター/ショップ名を選ぶビュー。"""

    def __init__(self, action: Literal["add", "remove"]):
        self.action = action
        super().__init__(timeout=180)

    @discord.ui.button(label="👤 アバター（URL / ID）", style=discord.ButtonStyle.primary)
    async def avatar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AvatarFilterModal(self.action))

    @discord.ui.button(label="🏪 ショップ名", style=discord.ButtonStyle.primary)
    async def shop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ShopFilterModal(self.action))


@bot.tree.command(name="filter", description="アバター(URL/ID)・ショップ名のフィルターを管理します")
async def filter_command(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    async with db_connect() as db:
        avatar_filters = await load_channel_filters(db, channel_id)
        shop_filters = await load_channel_shop_filters(db, channel_id)
        legacy_count = await count_legacy_name_filters(db, channel_id)
        if legacy_count:
            # 旧「アバター名」フィルターはID方式に置き換わったので掃除する
            await clear_legacy_name_filters(db, channel_id)

    legacy_notice = ""
    if legacy_count:
        legacy_notice = (
            f"\n♻️ 旧方式（アバター名）のフィルター {legacy_count} 件を削除したよ。\n"
            "これからは **アバターのBOOTH URL / 商品ID** で登録してね。"
        )

    if not avatar_filters and not shop_filters:
        await interaction.response.send_message(
            "📭 このチャンネルにはフィルターが登録されていないよ。\n"
            "「➕ フィルターを追加」ボタンから追加してね。\n"
            "アバターは **BOOTHのURL（末尾の7桁ID）** で指定するよ。"
            + legacy_notice,
            view=FilterActionSelect(),
            ephemeral=True,
        )
        return

    lines = ["📌 このチャンネルのフィルター一覧"]
    if avatar_filters:
        lines.append(f"\n👤 アバター（{len(avatar_filters)}件）")
        for entry in avatar_filters[:10]:
            lines.append(f"　• **{entry[1]}** — `{entry[0]}`")
        if len(avatar_filters) > 10:
            lines.append(f"　…ほか {len(avatar_filters) - 10} 件")
    if shop_filters:
        lines.append(f"\n🏪 ショップ名（{len(shop_filters)}件）")
        for display_name, _ in shop_filters[:10]:
            lines.append(f"　• `{display_name}`")
    lines.append("\n❌ ボタンを押すとそのフィルターを削除できるよ。")
    if legacy_notice:
        lines.append(legacy_notice)

    await interaction.response.send_message(
        "\n".join(lines),
        view=FilterListView(avatar_filters, shop_filters),
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
        # 既存の categories を保持しつつ、allow_nsfw のみ更新または新規登録
        await db.execute(
            """
            INSERT INTO channels (channel_id, guild_id, categories, allow_nsfw)
            VALUES (?, ?, COALESCE((SELECT categories FROM channels WHERE channel_id = ?), ''), ?)
            ON CONFLICT(channel_id) DO UPDATE SET allow_nsfw = excluded.allow_nsfw
            """,
            (channel_id, interaction.guild_id or 0, channel_id, allow),
        )
        await db.commit()

    status_text = "表示" if allow else "非表示"
    await interaction.response.send_message(
        f"🔞 このチャンネルでのR-18商品通知を **{status_text}** に設定したよ。",
        ephemeral=True,
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
        shop_filters = await load_channel_shop_filters(db, channel_id)

    if row is None:
        categories_text = "未設定"
        nsfw_text = "非表示（デフォルト）"
    else:
        categories_text = row[0] if row[0] else "未設定"
        nsfw_text = "表示" if row[1] else "非表示"

    filters_text = (
        "\n".join([f"• **{f[1]}** — `{f[0]}`" for f in filters[:10]]) if filters else "未登録"
    )
    if filters and len(filters) > 10:
        filters_text += f"\n…ほか {len(filters) - 10} 件"
    shop_filters_text = (
        ", ".join([f"`{f[0]}`" for f in shop_filters]) if shop_filters else "未登録"
    )

    embed = discord.Embed(
        title="📊 このチャンネルの設定",
        color=0xFF6473,
    )
    embed.add_field(name="通知カテゴリ", value=categories_text, inline=False)
    embed.add_field(name="R-18設定", value=nsfw_text, inline=False)
    embed.add_field(name="アバターフィルター（名前 — ID）", value=filters_text, inline=False)
    embed.add_field(name="ショップ名フィルター", value=shop_filters_text, inline=False)
    if filters or shop_filters:
        embed.add_field(
            name="ℹ️ フィルターの動作",
            value=(
                "登録したアバター / ショップに一致する商品**だけ**通知されるよ。\n"
                "アバターは商品説明に貼られた**アバターのBOOTH URL**で判定してるよ。"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="ℹ️ フィルターの動作",
            value="フィルター未登録なので、選んだジャンルの新作は**全部**通知されるよ。",
            inline=False,
        )
    embed.set_footer(text="BOOTH通知Bot / 詳しくは /help")

    await interaction.response.send_message(embed=embed, ephemeral=True)


INVITE_URL = (
    "https://discord.com/oauth2/authorize"
    "?client_id=1531860064061882368&permissions=2147503104"
    "&scope=bot%20applications.commands"
)


@bot.tree.command(name="help", description="このBotの使い方を表示します")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 BOOTH通知Bot の使い方",
        description=(
            "BOOTHのVRChat向け新作アイテムを自動でお知らせするBotだよ。\n"
            "5分に1回BOOTHをチェックして、公開から10分以内の新作を通知するよ。"
        ),
        color=0xFF6473,
    )
    embed.add_field(
        name="⚙️ 初期設定（3ステップ）",
        value=(
            "**1.** `/set-channel` — 通知チャンネルとジャンルを設定\n"
            "**2.** `/filter` — （任意）通知したいアバターを登録\n"
            "**3.** `/set-nsfw` — （任意）R-18の表示/非表示を切り替え"
        ),
        inline=False,
    )
    embed.add_field(
        name="📌 コマンド一覧",
        value=(
            "`/set-channel` — 通知チャンネルとジャンルを設定\n"
            "`/remove-channel` — このチャンネルの通知を解除\n"
            "`/filter` — フィルターの一覧 / 追加 / 削除\n"
            "`/set-nsfw allow|deny` — R-18の表示/非表示\n"
            "`/status` — 現在の設定を確認\n"
            "`/help` — このヘルプを表示"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 フィルターについて",
        value=(
            "フィルターは「通知を絞り込む」機能だよ。\n"
            "アバターは **BOOTHのURL（末尾の7桁ID）** で登録するよ。\n"
            "例: `https://booth.pm/ja/items/6106863` を登録 → "
            "商品説明にそのアバターのURLが貼られている新作だけ通知。\n"
            "通知には**アバター名**で表示されるよ。\n"
            "**未登録なら、選んだジャンルの新作は全部通知されるよ。**"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔗 このBotを他のサーバーにも入れる",
        value=f"[招待リンクはこちら]({INVITE_URL})",
        inline=False,
    )
    embed.set_footer(text="BOOTH通知Bot（非公式） / BOOTHの公式Botではありません")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Botの稼働状況を表示します（管理者用）")
async def stats_command(interaction: discord.Interaction):
    if MANAGER_USER_ID is None or interaction.user.id != MANAGER_USER_ID:
        await interaction.response.send_message(
            "❌ このコマンドは管理者のみ使用できます。", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    async with db_connect() as db:
        async with db.execute("SELECT COUNT(*) FROM channels") as cur:
            channel_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM avatar_filters") as cur:
            filter_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM shop_filters") as cur:
            shop_filter_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM items") as cur:
            item_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT MAX(notified_at) FROM items WHERE notified_at IS NOT NULL"
        ) as cur:
            last_notified = (await cur.fetchone())[0]
        failure_count = await get_failure_count(db)

    db_kind = "Turso" if (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN) else "ローカルSQLite"

    embed = discord.Embed(title="📊 Bot稼働状況", color=0x00BFFF)
    embed.add_field(name="参加サーバー数", value=f"{len(bot.guilds)}", inline=True)
    embed.add_field(name="通知チャンネル数", value=f"{channel_count}", inline=True)
    embed.add_field(name="レイテンシ", value=f"{bot.latency * 1000:.0f} ms", inline=True)
    embed.add_field(name="アバターフィルター", value=f"{filter_count}", inline=True)
    embed.add_field(name="ショップフィルター", value=f"{shop_filter_count}", inline=True)
    embed.add_field(name="蓄積アイテム数", value=f"{item_count}", inline=True)
    embed.add_field(name="データベース", value=db_kind, inline=True)
    embed.add_field(
        name="連続失敗回数",
        value=f"{failure_count}/{FAILURE_ALERT_THRESHOLD}",
        inline=True,
    )
    embed.add_field(name="最終通知", value=str(last_notified or "なし"), inline=False)
    embed.set_footer(text="BOOTH通知Bot 管理コマンド")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ───────────────────────────────────────────
# BOOTH 取得層
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
                    log.warn(
                        "fetch_with_retry",
                        f"⚠️ [fetch] レートリミット (429) を受信: {url} "
                        f"→ {retry_after:.0f}秒後にリトライ ({attempt}/{MAX_RETRIES})",
                    )
                    await asyncio.sleep(retry_after)
                    continue

                log.warn(
                    "fetch_with_retry",
                    f"⚠️ [fetch] HTTP {response.status}: {url} "
                    f"(試行 {attempt}/{MAX_RETRIES})",
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warn("fetch_with_retry", f"⚠️ [fetch] 通信エラー: {url} | {e} (試行 {attempt}/{MAX_RETRIES})")

        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    log.error("fetch_with_retry", f"❌ [fetch] 最大リトライ回数に到達: {url}")
    return None


async def fetch_item_json(session: aiohttp.ClientSession, item_id: str) -> dict | None:
    """商品JSON APIから詳細情報を取得する。"""
    url = f"https://booth.pm/ja/items/{item_id}.json"
    text = await fetch_with_retry(session, url)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warn("fetch_item_json", f"⚠️ [fetch_item_json] JSONデコード失敗 (ID: {item_id}): {e}")
        return None


async def fetch_booth_item(item_id: str) -> dict | None:
    """単発でBOOTH商品情報を取得する（フィルター登録時のアバター名解決用）。"""
    try:
        async with aiohttp.ClientSession(headers=BOOTH_REQUEST_HEADERS) as session:
            return await fetch_item_json(session, item_id)
    except Exception as e:
        log.error("fetch_booth_item", f"❌ 商品情報の取得に失敗 (ID: {item_id}): {e}")
        return None


# ───────────────────────────────────────────
# 通知ロジック（Step 4 で実装）
# ───────────────────────────────────────────
async def run_check_booth_job():
    """
    実際の巡回処理。
    成功したら True、失敗したら False を返す。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    log.info("run_check_booth_job", f"\n🔍 --- 【巡回スタート】{now:%Y-%m-%d %H:%M:%S} ---")

    async with aiohttp.ClientSession(headers=BOOTH_REQUEST_HEADERS) as session:
        # 1. 検索ページから商品IDを収集
        all_item_ids: list[str] = []
        for page in range(1, SEARCH_PAGES + 1):
            url = SEARCH_URL_TEMPLATE.format(page=page)
            html = await fetch_with_retry(session, url)
            if html is None:
                continue
            ids = parse_search_page(html)
            log.info("run_check_booth_job", f"📄 検索ページ {page}: {len(ids)} 件の商品IDを取得")
            for item_id in ids:
                if item_id not in all_item_ids:
                    all_item_ids.append(item_id)
            await asyncio.sleep(0.5)

        log.info("run_check_booth_job", f"📦 重複除去後: {len(all_item_ids)} 件")

        # 検索結果がゼロなら失敗とみなす（BOOTH構造変更の可能性）
        if not all_item_ids:
            log.error("run_check_booth_job", "❌ 検索結果から商品IDが1件も取得できませんでした")
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
            log.info("run_check_booth_job", f"🆕 新作判定: {len(new_items)} 件")

            # 3. 通知処理（フィルターは1回だけ読み込む）
            all_avatar_filters, all_shop_filters = await load_all_channel_filters(db)
            for item in new_items:
                try:
                    await broadcast_item(item, db, all_avatar_filters, all_shop_filters)
                    await db.execute(
                        "UPDATE items SET notified_at = ? WHERE item_id = ?",
                        (now, item["item_id"]),
                    )
                    await db.commit()
                except Exception as e:
                    log.error("run_check_booth_job", f"❌ [check_booth_job] 通知エラー (ID: {item['item_id']}): {e}")

    log.info("run_check_booth_job", f"🔍 --- 【巡回完了】{len(new_items)} 件通知 ---\n")
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
            log.error("check_booth_job", f"❌ [check_booth_job] 巡回中に例外が発生: {e}")
            success = False

        if success:
            # 成功したら失敗カウントをリセット
            if failure_count != 0:
                await set_failure_count(db, 0)
                log.info("check_booth_job", "✅ 巡回に成功したので失敗カウントをリセット")
            return

        # 失敗したらカウントを増やす
        failure_count += 1
        await set_failure_count(db, failure_count)
        log.warn("check_booth_job", f"⚠️ 巡回失敗。連続失敗回数: {failure_count}/{FAILURE_ALERT_THRESHOLD}")

        # 連続失敗が閾値を超えたら警告を出す（閾値以降は毎回通知）
        if failure_count >= FAILURE_ALERT_THRESHOLD:
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


@tasks.loop(minutes=1)
async def send_dm_replies():
    """dm_outbox に溜まった返信を各ユーザーにDM送信する。"""
    try:
        async with db_connect() as db:
            async with db.execute(
                "SELECT id, user_id, content FROM dm_outbox WHERE sent_at IS NULL ORDER BY id ASC"
            ) as cursor:
                rows = await cursor.fetchall()

            for row_id, user_id, content in rows:
                try:
                    user = await bot.fetch_user(user_id)
                    if user is None:
                        log.warn("send_dm_replies", f"⚠️ [dm_outbox] ユーザー取得失敗 (ID: {user_id})")
                        continue
                    await user.send(content)
                    log.info("send_dm_replies", f"📤 [DMブリッジ] {user.display_name} ({user_id}) に返信送信")
                    sent_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    await db.execute(
                        "UPDATE dm_outbox SET sent_at = ? WHERE id = ?",
                        (sent_at, row_id),
                    )
                    await db.commit()
                    await asyncio.sleep(0.5)
                except discord.Forbidden:
                    log.error("send_dm_replies", f"❌ [DMブリッジ] {user_id} への送信権限なし。10分後に再試行。")
                except discord.HTTPException as e:
                    log.error("send_dm_replies", f"❌ [DMブリッジ] HTTPエラー: {e}")
                except Exception as e:
                    log.error("send_dm_replies", f"❌ [DMブリッジ] 送信失敗: {e}")
    except Exception as e:
        log.error("send_dm_replies", f"❌ [DMブリッジ] ポーリングエラー: {e}")


@send_dm_replies.before_loop
async def before_send_dm_replies():
    await bot.wait_until_ready()


async def broadcast_item(
    item: dict,
    db: aiosqlite.Connection,
    all_avatar_filters: dict[int, list[tuple[str, str]]] | None = None,
    all_shop_filters: dict[int, list[tuple[str, str]]] | None = None,
):
    """対象チャンネルにEmbedを送信する。"""
    item_labels = get_item_labels(item)
    if not item_labels:
        return

    async with db.execute("SELECT channel_id, guild_id, categories, allow_nsfw FROM channels") as cursor:
        channels = await cursor.fetchall()

    if not channels:
        return

    # カテゴリバッジ
    badge_emojis = {"衣装": "👕", "髪": "💇", "小物": "🎀", "ギミック": "⚙️", "無料": "🆓"}
    badges = " ".join(f"`{badge_emojis.get(label, '✨')} {label}`" for label in item_labels)

    published_dt = datetime.datetime.fromisoformat(item["published_at"])

    embed = discord.Embed(
        description=(
            f"{badges}\n\n"
            f"**[{item['title']}]({item['url']})**\n"
            f"{item['shop_name'] or '不明'}"
        ),
        color=0xFF6B8A,
        timestamp=published_dt,
    )
    embed.add_field(name="💰 価格", value=item["price"], inline=True)
    embed.add_field(name="🏪 ショップ", value=item["shop_name"] or "不明", inline=True)
    embed.set_footer(
        text="BOOTH新作監視Bot",
        icon_url=(bot.user.display_avatar.url if bot.user and bot.user.display_avatar else None),
    )

    if item["image_url"]:
        embed.set_image(url=item["image_url"])

    # ボタン付きView
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="BOOTHで見る",
            url=item["url"],
            style=discord.ButtonStyle.link,
            emoji="🛒",
        )
    )
    if item.get("shop_url"):
        view.add_item(
            discord.ui.Button(
                label="ショップを見る",
                url=item["shop_url"],
                style=discord.ButtonStyle.link,
                emoji="🏪",
            )
        )

    for channel_id, guild_id, categories_str, allow_nsfw in channels:
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                log.info("broadcast_item", f"🗑️ 存在しないチャンネル (ID: {channel_id}) をDBから削除しました")
                await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
                await db.commit()
                continue
            except discord.Forbidden:
                log.warn("broadcast_item", f"⚠️ チャンネル (ID: {channel_id}) へのアクセス権限がありません")
                continue
            except Exception as e:
                log.error("broadcast_item", f"❌ チャンネル取得失敗 (ID: {channel_id}): {e}")
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

        # アバターフィルターチェック（BOOTH商品ID基準 / 名前は保険）
        if all_avatar_filters is not None:
            avatar_filters = all_avatar_filters.get(channel_id, [])
        else:
            avatar_filters = await load_channel_filters(db, channel_id)

        matched_avatar_filter = None
        avatar_match_reason = None
        if avatar_filters:
            try:
                tag_names = json.loads(item["tags"]) if item["tags"] else []
            except (ValueError, TypeError):
                tag_names = []
            try:
                linked_ids = json.loads(item.get("linked_item_ids") or "[]")
            except (ValueError, TypeError):
                linked_ids = []
            matched_avatar_filter, avatar_match_reason = match_avatar_filters(
                linked_ids, tag_names, item["title"], avatar_filters
            )

        # ショップ名フィルターチェック
        if all_shop_filters is not None:
            shop_filters = all_shop_filters.get(channel_id, [])
        else:
            shop_filters = await load_channel_shop_filters(db, channel_id)
        matched_shop_filter = None
        if shop_filters:
            shop_name_normalized = normalize_avatar_name(item["shop_name"] or "")
            for shop_name, normalized_name in shop_filters:
                if normalized_name in shop_name_normalized:
                    matched_shop_filter = shop_name
                    break

        # フィルターが登録されている場合、どちらかに一致しないと通知しない
        if (avatar_filters or shop_filters) and not matched_avatar_filter and not matched_shop_filter:
            continue

        # description にマッチしたフィルターを表示
        filter_lines = []
        if matched_avatar_filter:
            suffix = "" if avatar_match_reason == MATCH_BY_ID else "（名前一致）"
            filter_lines.append(f"🏷️ アバター: `{matched_avatar_filter}`{suffix}")
        if matched_shop_filter:
            filter_lines.append(f"🏪 ショップ: `{matched_shop_filter}`")
        filter_text = "\n".join(filter_lines)

        embed.description = (
            f"{badges}\n\n"
            f"**[{item['title']}]({item['url']})**\n"
            f"{item['shop_name'] or '不明'}"
        )
        if filter_text:
            embed.description += f"\n{filter_text}"

        try:
            await channel.send(embed=embed, view=view)
            log.info("broadcast_item", f"🚀 【送信成功】#{channel.name} に 「{item['title'][:20]}...」 を通知")
            await asyncio.sleep(0.3)
        except discord.Forbidden:
            log.error("broadcast_item", f"❌ 【送信失敗】#{channel.name} への送信権限がありません（次回巡回で再試行します）")
        except discord.HTTPException as e:
            log.error("broadcast_item", f"❌ 【送信HTTPエラー】#{channel.name}: {e}（次回巡回で再試行します）")
        except Exception as e:
            log.error("broadcast_item", f"❌ 【送信エラー】#{channel.name}: {e}（次回巡回で再試行します）")


# ───────────────────────────────────────────
# 起動イベント
# ───────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("on_ready", f"🎉 {bot.user.name} が正常に起動しました")

    # 環境変数バリデーション
    env_issues = validate_environment()
    if env_issues:
        log.warn("on_ready", "⚠️ 環境変数の問題:")
        for issue in env_issues:
            log.info("on_ready", f"   - {issue}")
        if MANAGER_USER_ID and DISCORD_TOKEN:
            await send_admin_alert(
                title="⚠️ 環境変数の問題があります",
                description="\n".join(f"- {issue}" for issue in env_issues),
                color=0xFFA500,
            )
    else:
        log.info("on_ready", "✅ 環境変数チェックOK")

    if ADMIN_CHANNEL_ID:
        log.info("on_ready", f"🔔 管理用警告チャンネル: {ADMIN_CHANNEL_ID}")
    if MANAGER_USER_ID:
        log.info("on_ready", f"👤 Managerユーザー: {MANAGER_USER_ID}")

    # DB接続確認
    db_ok = await validate_db_connection()
    if not db_ok:
        await send_admin_alert(
            title="🚨 DB接続に失敗しました",
            description="起動時のDB接続テストに失敗しました。Renderの環境変数を確認してください。",
        )
    elif not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        log.warn("on_ready", "⚠️ Turso未設定: ローカルSQLiteを使用中。Renderの無料枠ではデータが揮発する可能性があります。")


async def main():
    await start_web_server()
    if not DISCORD_TOKEN:
        log.warn("main", "⚠️ エラー: DISCORD_BOT_TOKEN が設定されていません")
        return
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
