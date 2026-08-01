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
    # 記号・空白を除去
    name = re.sub(r"[^\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\u3400-\u4dbf\w]", "", name)
    return name
