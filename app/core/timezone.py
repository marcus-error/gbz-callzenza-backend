import zoneinfo
from datetime import datetime, timezone, date as dt_date
from typing import Optional, Union, Tuple

# Application primary operational timezone (Asia/Kolkata / IST, UTC+05:30)
APP_TIMEZONE = zoneinfo.ZoneInfo("Asia/Kolkata")
UTC_TIMEZONE = timezone.utc


def get_current_ist_time() -> datetime:
    """Returns current datetime in Asia/Kolkata timezone."""
    return datetime.now(APP_TIMEZONE)


def get_current_utc_time() -> datetime:
    """Returns current naive UTC datetime for database storage."""
    return datetime.now(UTC_TIMEZONE).replace(tzinfo=None)


def get_ist_day_bounds_in_utc(date_str: Optional[str] = None) -> Tuple[datetime, datetime, dt_date]:
    """
    Given a date string 'YYYY-MM-DD' representing a day in Asia/Kolkata (or defaulting to today IST),
    calculates the UTC start and end bounds (naive datetimes) to query UTC-stored database records.

    Example for 2026-09-04 in IST:
      IST Day Start: 2026-09-04 00:00:00 IST -> UTC: 2026-09-03 18:30:00
      IST Day End:   2026-09-04 23:59:59.999999 IST -> UTC: 2026-09-04 18:29:59.999999
    """
    if date_str:
        try:
            target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.now(APP_TIMEZONE).date()
    else:
        target_date = datetime.now(APP_TIMEZONE).date()

    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=APP_TIMEZONE)
    end_local = datetime.combine(target_date, datetime.max.time(), tzinfo=APP_TIMEZONE)

    start_utc = start_local.astimezone(UTC_TIMEZONE).replace(tzinfo=None)
    end_utc = end_local.astimezone(UTC_TIMEZONE).replace(tzinfo=None)

    return start_utc, end_utc, target_date


def to_utc_iso(dt_val: Optional[Union[datetime, str]]) -> Optional[str]:
    """
    Converts a datetime or ISO string to a standardized UTC ISO-8601 string with 'Z'.
    If the datetime is naive (as stored in DB), it is assumed to be UTC.
    """
    if not dt_val:
        return None

    if isinstance(dt_val, str):
        try:
            # Handle ISO string with or without Z / offset
            cleaned = dt_val.strip()
            if cleaned.endswith("Z"):
                dt_val = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            elif "+" in cleaned or ("-" in cleaned[10:]):
                dt_val = datetime.fromisoformat(cleaned)
            else:
                dt_val = datetime.fromisoformat(cleaned).replace(tzinfo=UTC_TIMEZONE)
        except Exception:
            return dt_val

    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=UTC_TIMEZONE)
        else:
            dt_val = dt_val.astimezone(UTC_TIMEZONE)
        return dt_val.isoformat().replace("+00:00", "Z")

    return None


def to_ist_time_str(dt_val: Optional[Union[datetime, str]], fmt: str = "%I:%M:%S %p") -> str:
    """
    Converts a UTC database timestamp (naive or aware) to formatted Asia/Kolkata (IST) time string.
    """
    if not dt_val:
        return "-"

    if isinstance(dt_val, str):
        try:
            cleaned = dt_val.strip()
            if cleaned.endswith("Z"):
                dt_obj = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            elif "+" in cleaned or ("-" in cleaned[10:]):
                dt_obj = datetime.fromisoformat(cleaned)
            else:
                dt_obj = datetime.fromisoformat(cleaned).replace(tzinfo=UTC_TIMEZONE)
        except Exception:
            return dt_val
    elif isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_obj = dt_val.replace(tzinfo=UTC_TIMEZONE)
        else:
            dt_obj = dt_val.astimezone(UTC_TIMEZONE)
    else:
        return "-"

    ist_dt = dt_obj.astimezone(APP_TIMEZONE)
    return ist_dt.strftime(fmt)


def to_ist_datetime_str(dt_val: Optional[Union[datetime, str]], fmt: str = "%b %d, %Y %I:%M %p") -> str:
    """
    Converts a UTC database timestamp (naive or aware) to formatted Asia/Kolkata (IST) date-time string.
    """
    return to_ist_time_str(dt_val, fmt=fmt)
