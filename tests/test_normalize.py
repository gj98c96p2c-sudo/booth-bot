"""utils.normalize_avatar_name のテスト。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import normalize_avatar_name


def test_lowercase():
    assert normalize_avatar_name("Selestia") == normalize_avatar_name("selestia")


def test_katakana_hiragana():
    assert normalize_avatar_name("セレスティア") == normalize_avatar_name("せれすてぃあ")


def test_fullwidth_halfwidth():
    assert normalize_avatar_name("ＡＢＣ１２３") == normalize_avatar_name("abc123")


def test_symbols_removed():
    assert normalize_avatar_name("セレ スティア!!") == normalize_avatar_name("セレスティア")
    assert normalize_avatar_name("A-B_C") == normalize_avatar_name("ABC")


def test_kanji_preserved():
    assert "狐" in normalize_avatar_name("狐娘")


def test_empty():
    assert normalize_avatar_name("") == ""


def test_partial_match_usecase():
    """フィルターは部分一致で使われるので、正規化後も部分文字列が保たれること。"""
    tag = normalize_avatar_name("セレスティア用衣装")
    keyword = normalize_avatar_name("セレスティア")
    assert keyword in tag
