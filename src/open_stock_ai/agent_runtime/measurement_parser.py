"""Recognize notation that is not itself a financial measurement."""

from __future__ import annotations

import re


_CLOCK = (
    r"[Tt\s]+(?:[01]\d|2[0-3]):[0-5]\d"
    r"(?::[0-5]\d(?:\.\d+)?)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?"
)
_FULL_DATE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:"
    r"20\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])|"
    r"20\d{2}年\s*(?:0?[1-9]|1[0-2])月\s*(?:0?[1-9]|[12]\d|3[01])日"
    rf")(?:{_CLOCK})?|"
    # A bare eight-digit integer can be a real volume or financial amount.
    # Compact exchange dates are unambiguous only with their clock here.
    rf"20\d{{2}}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]){_CLOCK}"
    r")(?!\d)"
)
_MOVING_AVERAGE_WINDOW = re.compile(
    r"\b(?:SMA|EMA|WMA|MA)\s*\(?\s*\d{1,3}"
    r"(?:\s*[/／、]\s*\d{1,3})*\s*\)?"
    r"(?=\s*(?:[|:：=]|為|为|\bis\b|日|天|期|$))",
    re.IGNORECASE,
)
_NAMED_DAILY_WINDOW = re.compile(
    r"(?<![\d.])(?:\d{1,3}\s*[/／、]\s*)*\d{1,3}\s*日"
    r"(?=(?:均線|移動平均|年化波動率|報酬|高點|低點))"
)


def without_full_dates(value: str) -> str:
    """Remove complete dates/timestamps while retaining month-only periods."""
    return _FULL_DATE.sub(" ", value)


def without_indicator_windows(value: str) -> str:
    """Remove explicit window labels; values after the label remain measurable.

    In particular, ``RSI 48`` remains a numeric claim. Only named moving-average
    labels and explicit day-window notation are handled here.
    """
    return _NAMED_DAILY_WINDOW.sub(" ", _MOVING_AVERAGE_WINDOW.sub(" ", value))
