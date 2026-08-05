"""ユーティリティ関数。"""

import json
import re
import unicodedata


_KATAKANA_TO_HIRAGANA = str.maketrans(
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン"
    "ガギグゲゴザジズゼゾダヂヅデドバビブベボパピプペポ"
    "ァィゥェォャュョッヮ",
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
    "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽ"
    "ぁぃぅぇぉゃゅょっゎ",
)


def normalize_avatar_name(name: str) -> str:
    """
    アバター名を比較用に正規化する。
    大文字小文字・ひらがなカタカナ・全角半角・記号空白を同一視する。
    """
    # 小文字化
    name = name.lower()
    # Unicode正規化（全角英数字→半角 など）
    name = unicodedata.normalize("NFKC", name)
    # カタカナ → ひらがな
    name = name.translate(_KATAKANA_TO_HIRAGANA)
    # 記号・空白を除去（アンダースコアも区切り記号として扱う）
    name = re.sub(r"[^\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\u3400-\u4dbf\w]", "", name)
    name = name.replace("_", "")
    return name


MAX_FILTERS_PER_CHANNEL = 50    # 1チャンネルあたりのフィルター上限
MAX_FILTER_NAME_LENGTH = 50     # フィルター名の最大文字数
MIN_FILTER_NAME_LENGTH = 2      # フィルター名の最小文字数（正規化後）

# BOOTH の商品ID（URL末尾の数字）。現在は7桁だが将来の桁数変化に備えて幅を持たせる
BOOTH_ITEM_ID_MIN_DIGITS = 5
BOOTH_ITEM_ID_MAX_DIGITS = 10

_ITEM_ID_IN_URL = re.compile(r"items/(\d+)")
_ONLY_DIGITS = re.compile(r"^\d+$")


def extract_booth_item_id(raw: str) -> tuple[str | None, str | None]:
    """BOOTHのURL または 商品ID文字列から商品IDを取り出す。

    受け付ける形式:
        https://booth.pm/ja/items/1234567
        https://shop.booth.pm/items/1234567?xxx
        booth.pm/items/1234567
        1234567

    Returns:
        (item_id, error_message)
    """
    text = (raw or "").strip()

    if not text:
        return None, "⚠️ アバターのURL か 商品ID（末尾の数字）を入力してね。"

    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None, "⚠️ 使えない文字が含まれているよ。"

    # 全角数字なども半角へ寄せる
    text = unicodedata.normalize("NFKC", text)

    match = _ITEM_ID_IN_URL.search(text)
    if match:
        item_id = match.group(1)
    elif _ONLY_DIGITS.match(text):
        item_id = text
    else:
        return None, (
            "⚠️ アバターのBOOTH URL か 商品IDを入力してね。\n"
            "例: `https://booth.pm/ja/items/1234567` または `1234567`"
        )

    item_id = item_id.lstrip("0") or item_id

    if not (BOOTH_ITEM_ID_MIN_DIGITS <= len(item_id) <= BOOTH_ITEM_ID_MAX_DIGITS):
        return None, (
            f"⚠️ 商品IDの桁数がおかしいよ（{BOOTH_ITEM_ID_MIN_DIGITS}〜"
            f"{BOOTH_ITEM_ID_MAX_DIGITS}桁）。BOOTHのURL末尾の数字をそのまま入れてね。"
        )

    return item_id, None


# アバター名として意味を持たない定型ワード
_TITLE_NOISE_WORDS = (
    "オリジナル3Dモデル",
    "オリジナル3dモデル",
    "オリジナルアバター",
    "オリジナル3Dアバター",
    "3Dモデル",
    "3Dキャラクター",
    "VRChat想定",
    "VRChat向け",
    "VRChat対応",
    "VRC想定",
    "VRC向け",
)

_BRACKET_BLOCKS = re.compile(r"[【\[（(][^】\]）)]*[】\]）)]")
_QUOTED = re.compile(r"[「『\"“”]([^」』\"“”]{1,40})[」』\"“”]")
_DECORATIONS = "~〜◁▷◀▶＜＞<>-‐−–—_＿*＊✧✦★☆♡♥.,、。 　"
_LATIN_ALIAS = re.compile(r"[-‐−–—]\s*([A-Za-z][A-Za-z0-9 _']{1,24})\s*[-‐−–—]")


def clean_avatar_display_name(title: str) -> str:
    """BOOTHの商品名から通知に出すアバター名を抜き出す。

    例:
        'オリジナル3Dモデル「しなの」'            -> 'しなの'
        'オリジナル3Dモデル ~ネコチヤン~ #川井商店' -> 'ネコチヤン'
        '【オリジナル3Dモデル】 Sio / しお / ver.2' -> 'Sio'
    """
    text = (title or "").strip()
    if not text:
        return ""

    # 「」『』 で囲われていればそこがアバター名
    quoted = _QUOTED.search(text)
    if quoted:
        candidate = quoted.group(1).strip(_DECORATIONS).strip()
        if candidate:
            return candidate

    # 【...】 などのブロックを除去
    text = _BRACKET_BLOCKS.sub(" ", text)

    # 定型ワードを除去
    for word in _TITLE_NOISE_WORDS:
        text = text.replace(word, " ")

    # #タグ / スラッシュ区切りは最初の要素だけ使う
    for sep in ("#", "／", "/", "|", "｜"):
        if sep in text:
            head = text.split(sep)[0]
            if head.strip(_DECORATIONS).strip():
                text = head

    candidate = text.strip(_DECORATIONS).strip()
    # 空白区切りで複数残った場合は最初のかたまりを採用
    if candidate and (" " in candidate or "　" in candidate):
        parts = [p for p in re.split(r"[ 　]+", candidate) if p.strip(_DECORATIONS)]
        if parts:
            candidate = parts[0].strip(_DECORATIONS).strip()

    if not candidate:
        candidate = (title or "").strip()[:MAX_FILTER_NAME_LENGTH]

    return candidate[:MAX_FILTER_NAME_LENGTH]


def extract_name_aliases(title: str) -> list[str]:
    """商品名から別名（-kaguya- のような英字表記）を抜き出す。"""
    aliases: list[str] = []
    for raw in _LATIN_ALIAS.findall(title or ""):
        alias = raw.strip()
        if len(alias) >= 2 and alias not in aliases:
            aliases.append(alias)
    return aliases[:3]


MATCH_BY_ID = "id"
MATCH_BY_NAME = "name"


def match_avatar_filters(
    linked_item_ids: list[str],
    tags: list[str],
    title: str,
    avatar_filters: list[tuple],
) -> tuple[str | None, str | None]:
    """商品がアバターフィルターに一致するか判定する。

    Args:
        linked_item_ids: 商品説明文からリンクされていたBOOTH商品IDのリスト
        tags: 商品タグ
        title: 商品名
        avatar_filters: [(avatar_item_id, avatar_name, normalized_name, aliases_json), ...]

    Returns:
        (マッチしたアバター名, マッチ理由 'id' / 'name')。未一致なら (None, None)

    判定順:
        1. 商品説明に貼られた「対応アバター」URLの商品IDが一致（最優先・誤爆なし）
        2. アバター名（および英字別名）がタグ/商品名に含まれる（保険）
    """
    if not avatar_filters:
        return None, None

    linked = set(linked_item_ids or [])

    # 1. 商品IDでの厳密一致
    for entry in avatar_filters:
        avatar_item_id = str(entry[0])
        avatar_name = entry[1]
        if avatar_item_id in linked:
            return avatar_name, MATCH_BY_ID

    # 2. 名前ベースの保険マッチ
    haystack = [normalize_avatar_name(t) for t in (tags or [])]
    haystack.append(normalize_avatar_name(title or ""))
    haystack = [h for h in haystack if h]
    if not haystack:
        return None, None

    for entry in avatar_filters:
        avatar_name = entry[1]
        candidates = [entry[2]] if len(entry) > 2 and entry[2] else []
        aliases_raw = entry[3] if len(entry) > 3 else None
        if aliases_raw:
            try:
                for alias in json.loads(aliases_raw):
                    normalized_alias = normalize_avatar_name(alias)
                    if len(normalized_alias) >= MIN_FILTER_NAME_LENGTH:
                        candidates.append(normalized_alias)
            except (ValueError, TypeError):
                pass

        for candidate in candidates:
            if len(candidate) < MIN_FILTER_NAME_LENGTH:
                continue
            # 'Sio' のような短い英字名は部分一致だと誤爆する（fusion に sio が含まれる等）
            if candidate.isascii() and len(candidate) <= 3:
                if any(candidate == hay for hay in haystack):
                    return avatar_name, MATCH_BY_NAME
                continue
            if any(candidate in hay for hay in haystack):
                return avatar_name, MATCH_BY_NAME

    return None, None


def validate_filter_name(raw_name: str) -> tuple[str | None, str | None]:
    """フィルター名を検証する。

    Returns:
        (normalized_name, error_message)
        正常時は (正規化済み名前, None)、異常時は (None, エラーメッセージ)
    """
    name = raw_name.strip()

    if not name:
        return None, "⚠️ 名前が空だよ。"

    if len(name) > MAX_FILTER_NAME_LENGTH:
        return None, f"⚠️ 名前が長すぎるよ（{MAX_FILTER_NAME_LENGTH}文字以内にしてね）。"

    # 制御文字・改行を含むものは拒否
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, "⚠️ 使えない文字が含まれているよ。"

    # URLらしきものは拒否
    if "://" in name or name.lower().startswith("www."):
        return None, "⚠️ URLはフィルターに登録できないよ。"

    # Discordメンション/everyone は拒否
    if "@everyone" in name or "@here" in name or "<@" in name:
        return None, "⚠️ メンションはフィルターに登録できないよ。"

    normalized = normalize_avatar_name(name)

    if not normalized:
        return None, "⚠️ その名前ではフィルター登録できないよ（記号だけの名前は使えないよ）。"

    if len(normalized) < MIN_FILTER_NAME_LENGTH:
        return None, (
            f"⚠️ 名前が短すぎるよ（{MIN_FILTER_NAME_LENGTH}文字以上にしてね）。"
            "通知が多くなりすぎちゃうよ。"
        )

    return normalized, None
