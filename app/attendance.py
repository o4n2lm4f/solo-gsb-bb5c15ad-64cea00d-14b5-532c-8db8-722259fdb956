"""ATTENDEE response expansion for VEVENT instances.

Each :class:`~app.expander.Instance` produced by the recurrence expander
carries the *effective* ATTENDEE properties of the event that defines it:

* a master event's ATTENDEE list applies to **every** expanded instance of
  the recurring series;
* a ``RECURRENCE-ID`` override that declares at least one ATTENDEE
  **replaces** the master's whole attendee group for that instance;
* an override without any ATTENDEE inherits the master's group unchanged.

Because an override either adopts or replaces the group wholesale, the
effective list of every instance is always exactly one event's declared
list — never a merge.

Addresses are normalized: a ``mailto:`` prefix (any letter case) is
stripped and the remaining address is lowercased; any other URI is kept
verbatim. ``PARTSTAT`` defaults to ``NEEDS-ACTION`` and ``ROLE`` to
``REQ-PARTICIPANT``; both are uppercased. Within one effective list the
same address may appear only once per parameter combination: identical
re-declarations collapse into a single entry, while re-declarations whose
(normalized) ``PARTSTAT``/``ROLE`` disagree raise :class:`AttendanceError`,
surfaced to the client as HTTP 422. Like the alarms endpoint, every
VEVENT's declared ATTENDEE list is validated eagerly, so a conflicting
list yields 422 even when no instance of that event falls inside the
query window.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import (
    Instance,
    _parse_calendar,
    _props,
    expand_parsed_calendar,
)

UTC = dt.timezone.utc

DEFAULT_PARTSTAT = "NEEDS-ACTION"
DEFAULT_ROLE = "REQ-PARTICIPANT"

MAILTO_PREFIX = "mailto:"


class AttendanceError(Exception):
    """Invalid ATTENDEE input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class Attendee:
    """One normalized attendee of an instance's effective list."""

    address: str  # mailto: stripped and lowercased; other URIs verbatim
    partstat: str  # uppercased; NEEDS-ACTION when not declared
    role: str  # uppercased; REQ-PARTICIPANT when not declared


@dataclass(frozen=True)
class InstanceAttendance:
    """Attendance view of one expanded instance inside the query window."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    counts: dict[str, int]  # PARTSTAT -> number of attendees, keys sorted
    attendees: list[Attendee]  # sorted by address


def expand_attendance(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[InstanceAttendance]:
    """Expand all VEVENTs and return each in-window instance's attendance.

    Instances come from :func:`app.expander.expand_parsed_calendar`, so the
    result inherits its window filtering and (start, uid, rid) ordering.
    Every VEVENT's declared ATTENDEE list is validated eagerly (like the
    alarms endpoint validates every VALARM), so a conflicting duplicate
    address yields :class:`AttendanceError` even if the event has no
    instance inside the window.
    """
    calendar = _parse_calendar(ics_text)
    instances: list[Instance] = expand_parsed_calendar(
        calendar, window_start, window_end
    )

    for event in calendar.walk("VEVENT"):
        uid = event.get("UID")
        _attendee_list(_props(event, "ATTENDEE"), str(uid) if uid else "?")

    return [_instance_attendance(instance) for instance in instances]


# ---------------------------------------------------------------------------
# ATTENDEE parsing
# ---------------------------------------------------------------------------


def _instance_attendance(instance: Instance) -> InstanceAttendance:
    attendees = _attendee_list(instance.attendees, instance.uid)
    counts: dict[str, int] = {}
    for attendee in attendees:
        counts[attendee.partstat] = counts.get(attendee.partstat, 0) + 1
    return InstanceAttendance(
        uid=instance.uid,
        recurrence_id=instance.recurrence_id,
        start=instance.start,
        end=instance.end,
        counts={key: counts[key] for key in sorted(counts)},
        attendees=attendees,
    )


def _attendee_list(props: list, uid: str) -> list[Attendee]:
    """Normalize one event's ATTENDEE properties into a sorted list.

    The list is keyed by normalized address: re-declaring an address with
    identical PARTSTAT/ROLE collapses to one entry, while conflicting
    parameters are rejected.
    """
    by_address: dict[str, Attendee] = {}
    for prop in props:
        attendee = _parse_attendee(prop)
        existing = by_address.get(attendee.address)
        if existing is None:
            by_address[attendee.address] = attendee
        elif existing != attendee:
            raise AttendanceError(
                f"conflicting ATTENDEE parameters for {attendee.address!r} "
                f"in VEVENT (UID {uid})"
            )
    return sorted(by_address.values(), key=lambda a: a.address)


def _parse_attendee(prop) -> Attendee:
    params = getattr(prop, "params", None) or {}
    partstat = str(params.get("PARTSTAT") or DEFAULT_PARTSTAT).upper()
    role = str(params.get("ROLE") or DEFAULT_ROLE).upper()
    return Attendee(
        address=_normalize_address(str(prop)),
        partstat=partstat,
        role=role,
    )


def _normalize_address(value: str) -> str:
    """Strip a ``mailto:`` prefix (any case) and lowercase the address;
    any other URI is kept verbatim."""
    if value.lower().startswith(MAILTO_PREFIX):
        return value[len(MAILTO_PREFIX):].lower()
    return value
