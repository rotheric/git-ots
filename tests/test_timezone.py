from zoneinfo import ZoneInfo

import pytest

from git_ots.config import ConfigError, parse_timezone


@pytest.mark.parametrize(
    ("text", "expected_key"),
    [
        ("UTC", "UTC"),
        ("Europe/Berlin", "Europe/Berlin"),
    ],
)
def test_parse_timezone_supported(text, expected_key):
    tz = parse_timezone(text)
    assert isinstance(tz, ZoneInfo)
    assert tz.key == expected_key


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Not/AZone",
        "Mars/Olympus_Mons",
        "utc",
        "UTC+1",
        "+01:00",
        "Europe / Berlin",
        " Europe/Berlin",
        "Europe/Berlin ",
    ],
)
def test_parse_timezone_invalid(text):
    with pytest.raises(ConfigError):
        parse_timezone(text)
