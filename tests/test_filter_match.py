"""フィルターのマッチングロジックのテスト。

broadcast_item 内のマッチ判定と同じロジックを検証する。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import normalize_avatar_name


def match_avatar_filter(item_tags_json: str, filters: list[tuple[str, str]]) -> str | None:
    """broadcast_item と同じアバター名フィルター判定。"""
    if not filters:
        return None
    tag_names = json.loads(item_tags_json) if item_tags_json else []
    tag_names_normalized = [normalize_avatar_name(t) for t in tag_names]
    for avatar_name, normalized_name in filters:
        if any(normalized_name in tag_norm for tag_norm in tag_names_normalized):
            return avatar_name
    return None


def match_shop_filter(shop_name: str, filters: list[tuple[str, str]]) -> str | None:
    """broadcast_item と同じショップ名フィルター判定。"""
    if not filters:
        return None
    shop_normalized = normalize_avatar_name(shop_name or "")
    for name, normalized_name in filters:
        if normalized_name in shop_normalized:
            return name
    return None


def make_filter(name: str) -> tuple[str, str]:
    return (name, normalize_avatar_name(name))


# ── アバター名フィルター ─────────────────────

def test_avatar_filter_exact_tag():
    tags = json.dumps(["セレスティア", "VRChat"], ensure_ascii=False)
    assert match_avatar_filter(tags, [make_filter("セレスティア")]) == "セレスティア"


def test_avatar_filter_partial_tag():
    tags = json.dumps(["セレスティア用衣装"], ensure_ascii=False)
    assert match_avatar_filter(tags, [make_filter("セレスティア")]) == "セレスティア"


def test_avatar_filter_hiragana_katakana():
    tags = json.dumps(["せれすてぃあ"], ensure_ascii=False)
    assert match_avatar_filter(tags, [make_filter("セレスティア")]) == "セレスティア"


def test_avatar_filter_case_insensitive():
    tags = json.dumps(["Selestia"], ensure_ascii=False)
    assert match_avatar_filter(tags, [make_filter("selestia")]) == "selestia"


def test_avatar_filter_no_match():
    tags = json.dumps(["マヌカ", "VRChat"], ensure_ascii=False)
    assert match_avatar_filter(tags, [make_filter("セレスティア")]) is None


def test_avatar_filter_empty_filters_returns_none():
    tags = json.dumps(["セレスティア"], ensure_ascii=False)
    assert match_avatar_filter(tags, []) is None


def test_avatar_filter_empty_tags():
    assert match_avatar_filter("", [make_filter("セレスティア")]) is None


def test_avatar_filter_multiple_first_match_wins():
    tags = json.dumps(["マヌカ用衣装"], ensure_ascii=False)
    filters = [make_filter("セレスティア"), make_filter("マヌカ")]
    assert match_avatar_filter(tags, filters) == "マヌカ"


# ── ショップ名フィルター ─────────────────────

def test_shop_filter_exact():
    assert match_shop_filter("テストショップ", [make_filter("テストショップ")]) == "テストショップ"


def test_shop_filter_partial():
    assert match_shop_filter("テストショップ本店", [make_filter("テストショップ")]) == "テストショップ"


def test_shop_filter_no_match():
    assert match_shop_filter("別のお店", [make_filter("テストショップ")]) is None


def test_shop_filter_empty_shop_name():
    assert match_shop_filter("", [make_filter("テストショップ")]) is None


def test_shop_filter_no_filters():
    assert match_shop_filter("テストショップ", []) is None
