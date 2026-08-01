"""validate_filter_name のテスト。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import MAX_FILTER_NAME_LENGTH, validate_filter_name


def ok(name):
    normalized, error = validate_filter_name(name)
    assert error is None, f"{name!r} should be valid, got {error}"
    return normalized


def ng(name):
    normalized, error = validate_filter_name(name)
    assert normalized is None, f"{name!r} should be rejected"
    assert error is not None
    return error


# ── 正常系 ──────────────────────────────────

def test_valid_japanese():
    assert ok("セレスティア")


def test_valid_english():
    assert ok("Selestia")


def test_valid_with_spaces_trimmed():
    assert ok("  セレスティア  ")


def test_valid_mixed():
    assert ok("マヌカ v2")


def test_valid_max_length():
    assert ok("あ" * MAX_FILTER_NAME_LENGTH)


# ── 異常系 ──────────────────────────────────

def test_reject_empty():
    ng("")
    ng("   ")


def test_reject_too_long():
    ng("あ" * (MAX_FILTER_NAME_LENGTH + 1))


def test_reject_too_short_after_normalize():
    # 正規化後1文字は通知が多くなりすぎるので拒否
    ng("あ")
    ng("a")


def test_reject_symbols_only():
    ng("!!!")
    ng("---")


def test_reject_url():
    ng("https://booth.pm/ja/items/123")
    ng("www.example.com")


def test_reject_mention():
    ng("@everyone")
    ng("@here")
    ng("<@123456789>")


def test_reject_control_chars():
    ng("セレス\nティア")
    ng("セレス\x00ティア")


# ── 正規化結果 ──────────────────────────────

def test_normalized_output_matches():
    from utils import normalize_avatar_name
    normalized = ok("セレスティア")
    assert normalized == normalize_avatar_name("セレスティア")


def test_katakana_hiragana_same_normalized():
    assert ok("セレスティア") == ok("せれすてぃあ")
