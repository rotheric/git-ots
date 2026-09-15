from datetime import time

import pytest

from git_ots.config import ConfigError, parse_fixed_time


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        ("07:30", time(7, 30)),
    ],
)
def test_parse_fixed_time_supported(text, expected):
    assert parse_fixed_time(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0:00",
        "0:0",
        "7:5",
        "24:00",
        "12:60",
        "99:99",
        "junk",
        "12:345",
        "123:45",
        "12:45:00",
        " 12:45",
        "12:45 ",
    ],
)
def test_parse_fixed_time_invalid(text):
    with pytest.raises(ConfigError):
        parse_fixed_time(text)
