"""booth.py のパース処理テスト。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from booth import get_item_labels, map_category_label, parse_item_json, parse_search_page


# ── map_category_label ──────────────────────

def test_map_category_label_known():
    assert map_category_label("3D衣装") == "衣装"
    assert map_category_label("3D髪型") == "髪"
    assert map_category_label("3D装飾品") == "小物"
    assert map_category_label("3Dツール・システム") == "ギミック"


def test_map_category_label_unknown():
    assert map_category_label("3Dモデル") is None
    assert map_category_label("") is None


# ── get_item_labels ─────────────────────────

def test_get_item_labels_paid_costume():
    item = {"category_name": "3D衣装", "price": "¥ 1,500"}
    assert get_item_labels(item) == ["衣装"]


def test_get_item_labels_free_costume():
    item = {"category_name": "3D衣装", "price": "¥ 0"}
    assert set(get_item_labels(item)) == {"衣装", "無料"}


def test_get_item_labels_unknown_category_free():
    item = {"category_name": "音楽素材", "price": "¥ 0"}
    assert get_item_labels(item) == ["無料"]


def test_get_item_labels_unknown_category_paid():
    item = {"category_name": "音楽素材", "price": "¥ 500"}
    assert get_item_labels(item) == []


def test_get_item_labels_missing_keys():
    assert get_item_labels({}) == []


# ── parse_search_page ───────────────────────

SAMPLE_HTML = """
<ul>
  <li class="item-card"><a href="/ja/items/12345">A</a></li>
  <li class="item-card"><a href="/ja/items/67890">B</a></li>
  <li class="item-card"><a href="/ja/items/12345">dup</a></li>
  <li class="item-card"><span>no link</span></li>
</ul>
"""


def test_parse_search_page_extracts_ids():
    assert parse_search_page(SAMPLE_HTML) == ["12345", "67890"]


def test_parse_search_page_empty():
    assert parse_search_page("<html><body></body></html>") == []


# ── parse_item_json ─────────────────────────

SAMPLE_ITEM = {
    "name": "テスト衣装",
    "published_at": "2026-08-01T12:00:00.000+09:00",
    "price": "¥ 1,000",
    "wish_lists_count": 42,
    "is_adult": False,
    "images": [{"original": "https://example.com/a.png"}],
    "tags": [{"name": "セレスティア"}, {"name": "VRChat"}],
    "category": {"name": "3D衣装"},
    "shop": {"name": "テストショップ", "url": "https://testshop.booth.pm/"},
}


def test_parse_item_json_basic():
    result = parse_item_json("999", SAMPLE_ITEM, "衣装")
    assert result is not None
    assert result["item_id"] == "999"
    assert result["title"] == "テスト衣装"
    assert result["url"] == "https://booth.pm/ja/items/999"
    assert result["price"] == "¥ 1,000"
    assert result["category_name"] == "3D衣装"
    assert result["likes"] == 42
    assert result["is_adult"] == 0
    assert result["image_url"] == "https://example.com/a.png"
    assert result["shop_name"] == "テストショップ"
    assert result["shop_url"] == "https://testshop.booth.pm/"
    assert json.loads(result["tags"]) == ["セレスティア", "VRChat"]


def test_parse_item_json_missing_title():
    data = dict(SAMPLE_ITEM, name="")
    assert parse_item_json("1", data, "衣装") is None


def test_parse_item_json_missing_published_at():
    data = dict(SAMPLE_ITEM, published_at="")
    assert parse_item_json("1", data, "衣装") is None


def test_parse_item_json_adult_flag():
    data = dict(SAMPLE_ITEM, is_adult=True)
    assert parse_item_json("1", data, "衣装")["is_adult"] == 1


def test_parse_item_json_no_images():
    data = dict(SAMPLE_ITEM, images=[])
    assert parse_item_json("1", data, "衣装")["image_url"] == ""


def test_parse_item_json_no_shop():
    data = dict(SAMPLE_ITEM)
    del data["shop"]
    result = parse_item_json("1", data, "衣装")
    assert result["shop_name"] == ""
    assert result["shop_url"] == ""
