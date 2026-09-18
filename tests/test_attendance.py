"""Key tests for the /attendance endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//attendance-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def attendance(ics: str, window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z"):
    return client.post(
        "/attendance",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def weekly_master(*extra: str) -> str:
    return event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",  # Mondays Jan 5/12/19
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "SUMMARY:weekly sync",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com",
        "ATTENDEE:mailto:bob@example.com",
        *extra,
    )


# ---------------------------------------------------------------------------
# Basics: defaults, normalization, counts, sorting
# ---------------------------------------------------------------------------

def test_single_event_applies_defaults_and_counts():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED;ROLE=CHAIR:mailto:alice@example.com",
        "ATTENDEE:mailto:bob@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:carol@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    item = body["instances"][0]
    assert item["uid"] == "evt-1"
    assert item["recurrence_id"] is None
    assert item["start"] == "2026-01-05T09:00:00Z"
    assert item["end"] == "2026-01-05T10:00:00Z"
    assert item["counts"] == {"ACCEPTED": 1, "DECLINED": 1, "NEEDS-ACTION": 1}
    assert item["attendees"] == [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "CHAIR"},
        {"address": "bob@example.com", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"},
        {"address": "carol@example.com", "partstat": "DECLINED", "role": "REQ-PARTICIPANT"},
    ]


def test_mailto_prefix_stripped_case_insensitively_and_address_lowercased():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:MAILTO:Alice@Example.COM",
        "ATTENDEE:MailTo:Bob@Example.com",
    ))
    resp = attendance(ics)
    assert [a["address"] for a in resp.json()["instances"][0]["attendees"]] == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_non_mailto_uri_is_kept_verbatim():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:urn:uuid:Some-UUID",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:zoe@example.com",
    ))
    resp = attendance(ics)
    attendees = resp.json()["instances"][0]["attendees"]
    # The verbatim URI sorts before the mailto-derived address ("u" < "z").
    assert attendees == [
        {"address": "urn:uuid:Some-UUID", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"},
        {"address": "zoe@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
    ]


def test_partstat_and_role_values_are_uppercased():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=accepted;ROLE=opt-participant:mailto:alice@example.com",
    ))
    resp = attendance(ics)
    item = resp.json()["instances"][0]
    assert item["counts"] == {"ACCEPTED": 1}
    assert item["attendees"] == [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "OPT-PARTICIPANT"},
    ]


def test_attendees_sorted_by_address():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:zoe@example.com",
        "ATTENDEE:mailto:amy@example.com",
        "ATTENDEE:mailto:kim@example.com",
    ))
    resp = attendance(ics)
    assert [a["address"] for a in resp.json()["instances"][0]["attendees"]] == [
        "amy@example.com",
        "kim@example.com",
        "zoe@example.com",
    ]


def test_event_without_attendees_returns_empty_lists():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:no attendees here",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    item = resp.json()["instances"][0]
    assert item["counts"] == {}
    assert item["attendees"] == []


def test_instances_inherit_expand_ordering():
    ics = cal(
        event(
            "UID:b-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "ATTENDEE:mailto:bob@example.com",
        ),
        event(
            "UID:a-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "ATTENDEE:mailto:amy@example.com",
        ),
        event(
            "UID:c-event",
            "DTSTART:20260104T090000Z",
            "DTEND:20260104T100000Z",
        ),
    )
    resp = attendance(ics)
    assert [(i["start"], i["uid"]) for i in resp.json()["instances"]] == [
        ("2026-01-04T09:00:00Z", "c-event"),
        ("2026-01-05T10:00:00Z", "a-event"),
        ("2026-01-05T10:00:00Z", "b-event"),
    ]


# ---------------------------------------------------------------------------
# Recurrence: inheritance and override replacement
# ---------------------------------------------------------------------------

def test_master_attendees_apply_to_every_expanded_instance():
    resp = attendance(cal(weekly_master()))
    assert resp.status_code == 200
    instances = resp.json()["instances"]
    assert [i["recurrence_id"] for i in instances] == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
        "2026-01-19T09:00:00Z",
    ]
    for item in instances:
        assert item["counts"] == {"ACCEPTED": 1, "NEEDS-ACTION": 1}
        assert [a["address"] for a in item["attendees"]] == [
            "alice@example.com",
            "bob@example.com",
        ]


def test_override_with_attendees_replaces_the_whole_group():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "ATTENDEE;PARTSTAT=TENTATIVE:mailto:carol@example.com",
        ),
    )
    resp = attendance(ics)
    instances = {i["recurrence_id"]: i for i in resp.json()["instances"]}
    moved = instances["2026-01-12T09:00:00Z"]
    assert moved["start"] == "2026-01-12T11:00:00Z"
    # The master's alice/bob are gone; only the override's group remains.
    assert moved["counts"] == {"TENTATIVE": 1}
    assert [a["address"] for a in moved["attendees"]] == ["carol@example.com"]
    # The other instances still inherit the master's group.
    assert instances["2026-01-05T09:00:00Z"]["counts"] == {
        "ACCEPTED": 1,
        "NEEDS-ACTION": 1,
    }


def test_override_without_attendees_inherits_the_masters_group():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "SUMMARY:weekly sync (moved)",
        ),
    )
    resp = attendance(ics)
    instances = {i["recurrence_id"]: i for i in resp.json()["instances"]}
    moved = instances["2026-01-12T09:00:00Z"]
    assert moved["counts"] == {"ACCEPTED": 1, "NEEDS-ACTION": 1}
    assert [a["address"] for a in moved["attendees"]] == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_cancelled_override_produces_no_instance():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = attendance(ics)
    rids = [i["recurrence_id"] for i in resp.json()["instances"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_override_only_group_uses_its_own_attendees():
    ics = cal(event(
        "UID:evt-1",
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260112T110000Z",
        "DTEND:20260112T120000Z",
        "ATTENDEE:mailto:dave@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    item = resp.json()["instances"][0]
    assert [a["address"] for a in item["attendees"]] == ["dave@example.com"]


# ---------------------------------------------------------------------------
# Duplicates within one effective list
# ---------------------------------------------------------------------------

def test_identical_duplicate_address_collapses_to_one_entry():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=ACCEPTED:MAILTO:Alice@Example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    item = resp.json()["instances"][0]
    assert item["counts"] == {"ACCEPTED": 1}
    assert item["attendees"] == [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
    ]


def test_duplicate_with_defaulted_parameters_matching_collapses():
    # One declaration omits PARTSTAT/ROLE, the other spells out the defaults.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:alice@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    assert resp.json()["instances"][0]["attendees"] == [
        {"address": "alice@example.com", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"},
    ]


def test_conflicting_partstat_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:alice@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "alice@example.com" in resp.json()["detail"]


def test_conflicting_role_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;ROLE=CHAIR:mailto:alice@example.com",
        "ATTENDEE:mailto:alice@example.com",  # default ROLE=REQ-PARTICIPANT
    ))
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "alice@example.com" in resp.json()["detail"]


def test_conflict_in_override_list_returns_422():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:carol@example.com",
            "ATTENDEE;PARTSTAT=DECLINED:mailto:carol@example.com",
        ),
    )
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "carol@example.com" in resp.json()["detail"]


def test_conflicting_event_outside_window_still_returns_422():
    # Validation is eager for every VEVENT's declared ATTENDEE list, even
    # when the event has no instance inside the query window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260305T090000Z",
        "DTEND:20260305T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:MAILTO:Alice@Example.com",
    ))
    resp = attendance(
        ics,
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-02-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "alice@example.com" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Window filtering and shared validation
# ---------------------------------------------------------------------------

def test_only_instances_overlapping_the_window_are_returned():
    ics = cal(
        event(
            "UID:early",
            "DTSTART:20251105T090000Z",
            "DTEND:20251105T100000Z",
            "ATTENDEE:mailto:amy@example.com",
        ),
        event(
            "UID:inside",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "ATTENDEE:mailto:bob@example.com",
        ),
        event(
            "UID:late",
            "DTSTART:20260305T090000Z",
            "DTEND:20260305T100000Z",
            "ATTENDEE:mailto:kim@example.com",
        ),
    )
    resp = attendance(ics)
    assert resp.status_code == 200
    assert [i["uid"] for i in resp.json()["instances"]] == ["inside"]


def test_window_without_instances_returns_empty_response():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:amy@example.com",
    ))
    resp = attendance(
        ics,
        window_start="2026-03-01T00:00:00Z",
        window_end="2026-04-01T00:00:00Z",
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-04-01T00:00:00Z",
        "count": 0,
        "instances": [],
    }


def test_calendar_level_failures_still_return_422():
    resp = attendance("not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]

    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:amy@example.com",
    ))
    resp = attendance(ics, "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]
