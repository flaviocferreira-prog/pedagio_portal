from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SAO_PAULO_TIMEZONE_NAME = "America/Sao_Paulo"


def get_sao_paulo_timezone() -> tzinfo:
    """Return the IANA São Paulo timezone, with a safe Windows fallback.

    ``tzdata`` is the required source of IANA timezone data.  The fixed offset
    is only used when an incomplete installation cannot provide that database,
    so reading a conference never fails with HTTP 500.
    """
    try:
        return ZoneInfo(SAO_PAULO_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3), name=SAO_PAULO_TIMEZONE_NAME)


SAO_PAULO_TIMEZONE = get_sao_paulo_timezone()
