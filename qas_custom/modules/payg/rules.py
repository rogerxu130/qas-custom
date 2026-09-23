"""Brisbane calendar and booking rules; no persistence or Frappe dependency."""
import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


BRISBANE = ZoneInfo("Australia/Brisbane")


class RuleError(ValueError):
    pass


class PreviewStale(RuleError):
    pass


def as_brisbane_datetime(value):
    """Interpret Frappe's naive DATETIME values as Brisbane wall time.

    Aware values are converted to Brisbane. This is the only adapter for
    Frappe DATETIME inputs; business rules then compare aware datetimes.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise RuleError("Expected a DATETIME value")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=BRISBANE)
    return value.astimezone(BRISBANE)


def _date(value):
    if isinstance(value, datetime):
        return as_brisbane_datetime(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise RuleError("Expected a DATE value")


def add_six_months(issued_on):
    issued = _date(issued_on)
    month_index = issued.year * 12 + issued.month - 1 + 6
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(issued.day, calendar.monthrange(year, month)[1]))


def expiry_end(expires_on):
    return datetime.combine(_date(expires_on), time.max, tzinfo=BRISBANE)


def can_book(*, now, session_start):
    now = as_brisbane_datetime(now)
    start = as_brisbane_datetime(session_start)
    return now <= start - timedelta(hours=72)


def can_parent_cancel(*, now, session_start, booking_status):
    if booking_status not in ("Reserved", "Locked"):
        return False
    now = as_brisbane_datetime(now)
    start = as_brisbane_datetime(session_start)
    return now < start - timedelta(hours=72)


def _field(card, name):
    return card[name] if isinstance(card, dict) else getattr(card, name)


def card_covers_session(card, *, now, session_start):
    now = as_brisbane_datetime(now)
    start = as_brisbane_datetime(session_start)
    issued_on = _date(_field(card, "issued_on"))
    expires_on = _date(_field(card, "expires_on"))
    return (_field(card, "status") == "Active"
            and _field(card, "available_count") > 0
            and issued_on <= now.date() <= expires_on
            and issued_on <= start.date()
            and start <= expiry_end(expires_on))


def choose_card(cards, *, now, session_start):
    """Preview the earliest expiry/issue/name card usable for the session."""
    eligible = [card for card in cards if card_covers_session(card, now=now,
                                                               session_start=session_start)]
    eligible.sort(key=lambda card: (_date(_field(card, "expires_on")),
                                    _date(_field(card, "issued_on")),
                                    _field(card, "name")))
    return eligible[0] if eligible else None


def confirm_preview_card(cards, *, now, session_start, preview_card):
    """Re-select at confirmation and require the exact card shown in preview."""
    if not preview_card:
        raise PreviewStale("PAYG preview card is required; refresh the booking preview")
    chosen = choose_card(cards, now=now, session_start=session_start)
    if not chosen or _field(chosen, "name") != preview_card:
        raise PreviewStale("PAYG card selection changed; refresh the booking preview")
    return chosen
