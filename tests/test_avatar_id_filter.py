"""BOOTH商品ID の抽出とアバター名の整形のテスト。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from booth import extract_linked_item_ids
from utils import (
    clean_avatar_display_name,
    extract_booth_item_id,
    extract_name_aliases,
)


# ── extract_booth_item_id ─────────────────────

def test_id_from_full_url():
    assert extract_booth_item_id("https://booth.pm/ja/items/1234567") == ("1234567", None)


def test_id_from_shop_subdomain_url():
    assert extract_booth_item_id("https://shop.booth.pm/items/1234567") == ("1234567", None)


def test_id_from_url_with_query():
    item_id, error = extract_booth_item_id("https://booth.pm/ja/items/1234567?utm_source=x")
    assert item_id == "1234567"
    assert error is None


def test_id_from_bare_digits():
    assert extract_booth_item_id("1234567") == ("1234567", None)


def test_id_from_fullwidth_digits():
    assert extract_booth_item_id("１２３４５６７") == ("1234567", None)


def test_id_with_surrounding_spaces():
    assert extract_booth_item_id("  1234567  ") == ("1234567", None)


def test_id_empty_is_error():
    item_id, error = extract_booth_item_id("")
    assert item_id is None
    assert error is not None


def test_id_text_is_error():
    item_id, error = extract_booth_item_id("セレスティア")
    assert item_id is None
    assert error is not None


def test_id_too_short_is_error():
    item_id, error = extract_booth_item_id("123")
    assert item_id is None
    assert error is not None


def test_id_too_long_is_error():
    item_id, error = extract_booth_item_id("123456789012")
    assert item_id is None
    assert error is not None


def test_id_control_char_is_error():
    item_id, error = extract_booth_item_id("1234567\n@everyone")
    assert item_id is None
    assert error is not None


# ── clean_avatar_display_name ─────────────────────

def test_clean_name_quoted():
    assert clean_avatar_display_name("オリジナル3Dモデル「しなの」") == "しなの"


def test_clean_name_quoted_with_latin_suffix():
    assert clean_avatar_display_name("九尾オリジナル3Dモデル「輝夜」-kaguya-") == "輝夜"


def test_clean_name_tilde():
    assert clean_avatar_display_name("オリジナル3Dモデル ~ネコチヤン~ #ネコチヤン3D #川井商店") == "ネコチヤン"


def test_clean_name_triangle_brackets():
    assert clean_avatar_display_name("オリジナル3Dモデル ◁フォシュニア▷") == "フォシュニア"


def test_clean_name_slash_separated():
    assert clean_avatar_display_name(" 【オリジナル3Dモデル】 Sio / しお / ver.2.01") == "Sio"


def test_clean_name_empty():
    assert clean_avatar_display_name("") == ""


def test_clean_name_fallback_keeps_something():
    assert clean_avatar_display_name("【【【】】】") != ""


# ── extract_name_aliases ─────────────────────

def test_aliases_latin():
    assert "kaguya" in extract_name_aliases("九尾オリジナル3Dモデル「輝夜」-kaguya-")


def test_aliases_none():
    assert extract_name_aliases("オリジナル3Dモデル「しなの」") == []


# ── extract_linked_item_ids ─────────────────────

def test_linked_ids_from_description():
    data = {
        "description": (
            "対応アバター\n"
            "しなの: https://booth.pm/ja/items/6106863\n"
            "マヌカ: https://booth.pm/ja/items/5058077\n"
        )
    }
    assert extract_linked_item_ids(data) == ["6106863", "5058077"]


def test_linked_ids_dedup():
    data = {
        "description": "https://booth.pm/ja/items/6106863 と https://booth.pm/en/items/6106863"
    }
    assert extract_linked_item_ids(data) == ["6106863"]


def test_linked_ids_shop_subdomain():
    data = {"description": "https://ponderonium.booth.pm/items/6106863"}
    assert extract_linked_item_ids(data) == ["6106863"]


def test_linked_ids_includes_factory_description():
    data = {"description": "", "factory_description": "https://booth.pm/ja/items/1111111"}
    assert extract_linked_item_ids(data) == ["1111111"]


def test_linked_ids_empty():
    assert extract_linked_item_ids({}) == []


def test_linked_ids_ignores_other_urls():
    data = {"description": "https://example.com/items/1234567"}
    assert extract_linked_item_ids(data) == []
