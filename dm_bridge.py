#!/usr/bin/env python3
"""
BoothBOT Manager 用 DM ブリッジ CLI

Bot への DM を確認し、返信を投入するためのスクリプト。
Turso またはローカル SQLite の dm_inbox / dm_outbox テーブルを操作する。

使い方:
    # 未読DMを確認
    python dm_bridge.py --check

    # ユーザーに返信を投入（Botが1分以内にDM送信）
    python dm_bridge.py --reply 123456789 "こんにちは！"

    # 過去のDMも含めて全件表示
    python dm_bridge.py --check --all
"""

import argparse
import asyncio
import datetime
import json
import os
import sys

# main.py と同じDB接続を使う
from main import db_connect


async def list_inbox(show_all: bool = False):
    """dm_inbox の内容を表示する。"""
    async with db_connect() as db:
        if show_all:
            async with db.execute(
                "SELECT id, user_id, username, display_name, content, attachments, created_at, replied "
                "FROM dm_inbox ORDER BY id DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT id, user_id, username, display_name, content, attachments, created_at, replied "
                "FROM dm_inbox WHERE replied = 0 ORDER BY id ASC"
            ) as cursor:
                rows = await cursor.fetchall()

    if not rows:
        print("📭 新着DMはありません。")
        return

    print(f"\n📬 {'未読' if not show_all else '全ての'} DM ({len(rows)}件)")
    print("=" * 70)
    for row in rows:
        row_id, user_id, username, display_name, content, attachments_json, created_at, replied = row
        attachments = json.loads(attachments_json or "[]")
        status = "✅ 返信済" if replied else "📩 未読"
        print(f"\n[{row_id}] {status} {created_at}")
        print(f"    ユーザー: {display_name} (@{username}) / ID: {user_id}")
        print(f"    内容: {content}")
        if attachments:
            print(f"    添付: {', '.join(attachments)}")
        if not replied:
            print(f"    💡 返信: python dm_bridge.py --reply {user_id} \"ここに返信\"")
    print("=" * 70 + "\n")


async def send_reply(user_id: int, message: str):
    """dm_outbox に返信を追加する。"""
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO dm_outbox (user_id, content, created_at) VALUES (?, ?, ?)",
            (user_id, message, created_at),
        )
        await db.commit()

    print(f"✅ ユーザーID {user_id} への返信を outbox に投入しました。")
    print(f"   内容: {message}")
    print("   Botが1分以内にDM送信します。\n")


async def mark_replied(row_id: int):
    """指定したDMを返信済みにマークする。"""
    async with db_connect() as db:
        await db.execute(
            "UPDATE dm_inbox SET replied = 1 WHERE id = ?",
            (row_id,),
        )
        await db.commit()
    print(f"✅ DM ID {row_id} を返信済みにマークしました。")


def main():
    parser = argparse.ArgumentParser(description="BoothBOT DM ブリッジ CLI")
    parser.add_argument("--check", action="store_true", help="未読DMを表示")
    parser.add_argument("--all", action="store_true", help="--check と一緒に使い、全件表示")
    parser.add_argument("--reply", type=int, metavar="USER_ID", help="返信先のDiscordユーザーID")
    parser.add_argument("message", nargs="?", help="返信メッセージ")
    parser.add_argument("--mark-replied", type=int, metavar="ID", help="指定DMを返信済みにする")

    args = parser.parse_args()

    if args.mark_replied is not None:
        asyncio.run(mark_replied(args.mark_replied))
        return

    if args.reply is not None:
        if not args.message:
            print("❌ --reply には message 引数が必要です。")
            print('   例: python dm_bridge.py --reply 123456789 "こんにちは！"')
            sys.exit(1)
        asyncio.run(send_reply(args.reply, args.message))
        return

    if args.check or args.all:
        asyncio.run(list_inbox(show_all=args.all))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
