"""BOOTH のスクレイピング・パース・カテゴリ判定。"""

import json
import re

import bs4

import logging_utils as log

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

# アバター本体（3Dモデル）のBOOTHカテゴリ名
AVATAR_CATEGORY_NAMES = ("3Dキャラクター", "3Dモデル（その他）")

# 商品説明文に含まれる BOOTH 商品URL（対応アバターのリンク）
BOOTH_ITEM_URL_PATTERN = re.compile(r"booth\.pm/(?:[A-Za-z-]+/)?items/(\d+)")


def extract_linked_item_ids(data: dict) -> list[str]:
    """商品説明文からリンクされているBOOTH商品IDを抽出する。

    出品者が「対応アバター」として貼っているURLを拾うのが目的。
    """
    parts = [
        data.get("description") or "",
        data.get("factory_description") or "",
    ]
    ids: list[str] = []
    for found in BOOTH_ITEM_URL_PATTERN.findall(" ".join(parts)):
        if found not in ids:
            ids.append(found)
    return ids


def map_category_label(category_name: str) -> str | None:
    """カテゴリ名からユーザー向けラベル（衣装/髪/小物/ギミック）を返す。"""
    for label, booth_names in CATEGORY_LABELS.items():
        if category_name in booth_names:
            return label
    return None


def get_item_labels(item: dict) -> list[str]:
    """商品に対応する通知カテゴリのリストを返す（無料は追加）。"""
    labels: list[str] = []
    base_label = map_category_label(item.get("category_name", ""))
    if base_label:
        labels.append(base_label)
    if item.get("price") == "¥ 0":
        labels.append("無料")
    return labels


def parse_search_page(html: str) -> list[str]:
    """検索結果ページから商品IDのリストを抽出する。"""
    soup = bs4.BeautifulSoup(html, "html.parser")
    cards = soup.select("li.item-card") or soup.select(".item-card")

    if not cards:
        log.warn("parse_search_page", "⚠️ [parse_search_page] アイテム要素が見つかりません — BOOTHのHTML構造が変わった可能性があります")
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


def parse_item_json(item_id: str, data: dict, category_label: str) -> dict | None:
    """商品JSONをBot内部形式に変換する。"""
    title = data.get("name", "").strip()
    published_at = data.get("published_at", "").strip()

    if not title:
        log.warn("parse_item_json", f"⚠️ [parse_item_json] タイトルが無い商品をスキップ: ID={item_id}")
        return None
    if not published_at:
        log.warn("parse_item_json", f"⚠️ [parse_item_json] 公開日時が無い商品をスキップ: ID={item_id}")
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
        "category": category_label,
        "category_name": category_name,
        "likes": int(data.get("wish_lists_count", 0) or 0),
        "image_url": image_url,
        "is_adult": 1 if data.get("is_adult") else 0,
        "published_at": published_at,
        "shop_name": data.get("shop", {}).get("name", "") if isinstance(data.get("shop"), dict) else "",
        "shop_url": data.get("shop", {}).get("url", "") if isinstance(data.get("shop"), dict) else "",
        "tags": json.dumps(tags, ensure_ascii=False),
        "linked_item_ids": json.dumps(extract_linked_item_ids(data)),
    }
