"""フィルターのマッチングロジックのテスト。

broadcast_item 内のマッチ判定と同じロジックを検証する。
アバターフィルターは BOOTH商品ID 基準（名前は保険）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (
    MATCH_BY_ID,
    MATCH_BY_NAME,
    clean_avatar_display_name,
    extract_name_aliases,
    match_avatar_filters,
    normalize_avatar_name,
)


def make_avatar_filter(item_id: str, title: str) -> tuple[str, str, str, str]:
    """フィルター登録時と同じ形でレコードを作る。"""
    display_name = clean_avatar_display_name(title)
    return (
        item_id,
        display_name,
        normalize_avatar_name(display_name),
        json.dumps(extract_name_aliases(title), ensure_ascii=False),
    )


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


SHINANO = make_avatar_filter("6106863", "オリジナル3Dモデル「しなの」")
KAGUYA = make_avatar_filter("8562330", "九尾オリジナル3Dモデル「輝夜」-kaguya-")


# ── アバターフィルター: 商品IDマッチ ─────────────────────

def test_avatar_filter_matches_linked_item_id():
    name, reason = match_avatar_filters(
        ["6106863"], ["VRChat", "衣装"], "新作衣装", [SHINANO]
    )
    assert name == "しなの"
    assert reason == MATCH_BY_ID


def test_avatar_filter_id_match_wins_over_name():
    """IDが一致したフィルターが優先される。"""
    name, reason = match_avatar_filters(
        ["8562330"], ["しなの"], "衣装", [SHINANO, KAGUYA]
    )
    assert name == "輝夜"
    assert reason == MATCH_BY_ID


def test_avatar_filter_unrelated_id_no_match():
    name, reason = match_avatar_filters(
        ["9999999"], ["VRChat"], "新作衣装", [SHINANO]
    )
    assert name is None
    assert reason is None


def test_avatar_filter_no_filters_returns_none():
    assert match_avatar_filters(["6106863"], ["しなの"], "衣装", []) == (None, None)


# ── アバターフィルター: 名前の保険マッチ ─────────────────────

def test_avatar_filter_name_fallback_from_tag():
    """URLが貼られていなくてもタグ名で拾う。"""
    name, reason = match_avatar_filters(
        [], ["しなの対応", "VRChat"], "衣装", [SHINANO]
    )
    assert name == "しなの"
    assert reason == MATCH_BY_NAME


def test_avatar_filter_name_fallback_from_title():
    name, reason = match_avatar_filters(
        [], ["VRChat"], "【輝夜対応】ぐるーみぃeyes", [KAGUYA]
    )
    assert name == "輝夜"
    assert reason == MATCH_BY_NAME


def test_avatar_filter_alias_fallback():
    """英字別名（-kaguya-）でも拾える。"""
    name, reason = match_avatar_filters(
        [], ["kaguya", "VRChat"], "makeup texture", [KAGUYA]
    )
    assert name == "輝夜"
    assert reason == MATCH_BY_NAME


def test_avatar_filter_hiragana_katakana_fallback():
    name, _ = match_avatar_filters([], ["シナノ"], "衣装", [SHINANO])
    assert name == "しなの"


def test_avatar_filter_no_match_at_all():
    name, reason = match_avatar_filters([], ["マヌカ"], "衣装", [SHINANO])
    assert name is None
    assert reason is None


def test_avatar_filter_empty_tags_and_title():
    assert match_avatar_filters([], [], "", [SHINANO]) == (None, None)


def test_avatar_filter_handles_broken_aliases_json():
    broken = ("6106863", "しなの", normalize_avatar_name("しなの"), "not-json")
    name, _ = match_avatar_filters([], ["しなの"], "衣装", [broken])
    assert name == "しなの"


def test_avatar_filter_accepts_int_item_id():
    """DBがINTEGERで返してきても比較できる。"""
    entry = (6106863, "しなの", normalize_avatar_name("しなの"), "[]")
    name, reason = match_avatar_filters(["6106863"], [], "衣装", [entry])
    assert name == "しなの"
    assert reason == MATCH_BY_ID


def test_short_ascii_name_does_not_partial_match():
    """'Sio' が 'fusion' に部分一致して誤爆しない。"""
    sio = make_avatar_filter("5650156", "【オリジナル3Dモデル】 Sio / しお / ver.2.01")
    assert sio[1] == "Sio"
    name, _ = match_avatar_filters([], ["fusion", "VRChat"], "衣装", [sio])
    assert name is None


def test_short_ascii_name_exact_tag_matches():
    sio = make_avatar_filter("5650156", "【オリジナル3Dモデル】 Sio / しお / ver.2.01")
    name, reason = match_avatar_filters([], ["Sio", "VRChat"], "衣装", [sio])
    assert name == "Sio"
    assert reason == MATCH_BY_NAME


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
