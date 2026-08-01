"""ユーティリティ関数。"""

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
