import pytest

from utils import normalize_avatar_name


@pytest.mark.parametrize(
    "input_name,expected",
    [
        (" セレスティア ", "せれすてぃあ"),
        ("セレスティア", "せれすてぃあ"),
        ("ｾﾚｽﾃｨｱ", "せれすてぃあ"),
        ("Celestia", "celestia"),
        ("Ｃｅｌｅｓｔｉａ", "celestia"),
        ("アイア / Iron", "あいあiron"),
        ("", ""),
    ],
)
def test_normalize_avatar_name(input_name, expected):
    assert normalize_avatar_name(input_name) == expected
