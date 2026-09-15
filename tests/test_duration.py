from datetime import timedelta

import pytest

from git_ots.config import ConfigError, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("45s", timedelta(seconds=45)),
        ("120s", timedelta(seconds=120)),
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("24h", timedelta(hours=24)),
        ("2d", timedelta(days=2)),
        ("7d", timedelta(days=7)),
    ],
)
def test_parse_duration_supported(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1w",
        "1.5h",
        "junk",
        "0s",
        "0m",
        "0h",
        "0d",
        "-5s",
        "-5m",
        "-1h",
    ],
)
def test_parse_duration_invalid(text):
    with pytest.raises(ConfigError):
        parse_duration(text)


@pytest.mark.parametrize("text", ["99999999999d", "1000000000000000s"])
def test_parse_duration_out_of_range_raises_config_error(text):
    """A magnitude large enough to overflow timedelta's C int fields must
    surface as ConfigError, not OverflowError -- cli.py's exception ladder
    only catches ConfigError, so an uncaught OverflowError would produce a
    traceback and exit 1 instead of the documented exit 2."""
    with pytest.raises(ConfigError):
        parse_duration(text)
