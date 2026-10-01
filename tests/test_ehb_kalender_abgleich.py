"""Tests für scripts/ehb_kalender_abgleich.py. Aufruf: python3 -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ehb_kalender_abgleich as ab  # noqa: E402

TODAY = date(2026, 10, 5)
TARGET = "primary"
JOSEPH_ID = "joseph@group.calendar.google.com"
SPAN = {"startTime": "2026-10-05T00:00:00+02:00", "endTime": "2027-11-09T00:00:00+01:00"}
MANAGED = {"calendarId": TARGET, "fullText": "EHB-Abgleich", **SPAN}
SOURCE = {"calendarId": JOSEPH_ID, **SPAN}


def vevent(uid, summary, start, end, description="", location=""):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", f"DTSTART;TZID=Europe/Berlin:{start}",
             f"DTEND;TZID=Europe/Berlin:{end}"]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if location:
        lines.append(f"LOCATION:{location}")
    return lines + ["END:VEVENT"]


def ics(*events):
    return "\r\n".join(["BEGIN:VCALENDAR"] + [line for e in events for line in e] + ["END:VCALENDAR"]) + "\r\n"


def copy(source, key, summary, start, end, description="", location="", event_id=None):
    text = (description + "\n\n" if description else "") + f"EHB-Abgleich: {source} ({key})"
    item = {"id": event_id or f"id-{key}", "summary": summary, "description": text, "status": "confirmed",
            "start": {"dateTime": start, "timeZone": "Europe/Berlin"}, "end": {"dateTime": end, "timeZone": "Europe/Berlin"}}
    if location:
        item["location"] = location
    return item


LECTURE = vevent("a@ehb", "Vorlesung A", "20261102T083000", "20261102T091500", "Dozent: X", "B 202")
LECTURE_COPY = copy("Gross B", "a@ehb", "Vorlesung A", "2026-11-02T08:30:00+01:00", "2026-11-02T09:15:00+01:00",
                    "Dozent: X", "B 202")


class Transcript:
    def __init__(self, folder):
        self.path = Path(folder) / "session.jsonl"
        self.lines = []
        self.n = 0

    def call(self, op, args, result=None, error=False):
        self.n += 1
        tid = f"toolu_{self.n}"
        self.lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__Google-Calendar__{op}", "input": args}]}})
        entry = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": json.dumps(result or {}), "is_error": error}]}}
        if isinstance(result, dict) and not error:
            entry["mcpMeta"] = {"structuredContent": result}
        self.lines.append(entry)
        return self

    def calls(self):
        self.path.write_text("\n".join(json.dumps(x) for x in self.lines) + "\n", encoding="utf-8")
        return ab.read_calls(self.path)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        folder = Path(self.dir.name)
        self.klein, self.gross = folder / "klein-1b.ics", folder / "gross-b.ics"
        self.klein.write_text(ics(), encoding="utf-8")
        self.gross.write_text(ics(LECTURE), encoding="utf-8")
        self.feeds = ab.FEEDS
        ab.FEEDS = [("Klein 1b", self.klein), ("Gross B", self.gross)]
        self.t = Transcript(folder)
        self.t.call("list_calendars", {}, {"calendars": [{"id": JOSEPH_ID, "summary": "EHB St. Joseph Termine"}]})

    def tearDown(self):
        ab.FEEDS = self.feeds
        self.dir.cleanup()

    def plan(self):
        return ab.plan(self.t.calls(), TODAY, TARGET, None)

    def listed(self, managed, joseph=()):
        self.t.call("list_events", dict(MANAGED, pageSize=250), {"events": list(managed)})
        self.t.call("list_events", dict(SOURCE, pageSize=250), {"events": list(joseph)})


class Reading(Base):
    def test_first_asks_for_the_calendar_list(self):
        result = ab.plan([], TODAY, TARGET, None)
        self.assertEqual(result["status"], "lesen")
        self.assertEqual(result["aufrufe"][0]["werkzeug"], "list_calendars")

    def test_lists_both_calendars_with_a_fixed_window(self):
        result = self.plan()
        self.assertEqual(result["status"], "lesen")
        self.assertEqual([c["argumente"] for c in result["aufrufe"]], [dict(MANAGED, pageSize=250), dict(SOURCE, pageSize=250)])

    def test_asks_for_the_next_page(self):
        self.t.call("list_events", dict(MANAGED, pageSize=250), {"events": [LECTURE_COPY], "nextPageToken": "p2"})
        self.t.call("list_events", dict(SOURCE, pageSize=250), {"events": []})
        result = self.plan()
        self.assertEqual(result["aufrufe"][0]["argumente"].get("pageToken"), "p2")

    def test_in_sync_means_no_changes(self):
        self.listed([LECTURE_COPY])
        result = self.plan()
        self.assertEqual(result["status"], "fertig")
        self.assertIn("0 neu, 0 geändert, 0 gestrichen", result["meldung"])


class Changes(Base):
    def setUp(self):
        super().setUp()
        new = vevent("b@ehb", "Vorlesung B", "20261103T123000", "20261103T154500")
        moved_away = copy("Gross B", "c@ehb", "Vorlesung C", "2026-11-04T08:30:00+01:00", "2026-11-04T11:45:00+01:00")
        kept = [copy("Gross B", f"k{i}@ehb", f"K{i}", f"2026-11-1{i}T08:30:00+01:00", f"2026-11-1{i}T09:00:00+01:00") for i in range(3)]
        changed = dict(LECTURE_COPY, description="Dozent: Y\n\nEHB-Abgleich: Gross B (a@ehb)")
        self.gross.write_text(ics(LECTURE, new, *[vevent(f"k{i}@ehb", f"K{i}", f"2026111{i}T083000", f"2026111{i}T090000") for i in range(3)]),
                              encoding="utf-8")
        self.listed([changed, moved_away, *kept])

    def test_new_event_needs_a_slot_check_first(self):
        result = self.plan()
        self.assertEqual(result["status"], "lesen")
        self.assertEqual(result["aufrufe"][0]["argumente"]["startTime"], "2026-11-03T12:30:00+01:00")

    def test_writes_create_update_delete(self):
        self.t.call("list_events", {"calendarId": TARGET, "startTime": "2026-11-03T12:30:00+01:00",
                                    "endTime": "2026-11-03T15:45:00+01:00", "pageSize": 250}, {"events": []})
        result = self.plan()
        self.assertEqual(result["status"], "schreiben")
        ops = {c["werkzeug"]: c["argumente"] for c in result["aufrufe"]}
        self.assertEqual(set(ops), {"create_event", "update_event", "delete_event"})
        self.assertTrue(ops["create_event"]["description"].endswith("EHB-Abgleich: Gross B (b@ehb)"))
        self.assertFalse(ops["create_event"]["useDefaultReminders"])
        self.assertEqual(ops["update_event"]["eventId"], "id-a@ehb")
        self.assertEqual(ops["update_event"]["description"], "Dozent: X\n\nEHB-Abgleich: Gross B (a@ehb)")
        self.assertEqual(ops["delete_event"], {"calendarId": TARGET, "eventId": "id-c@ehb", "notificationLevel": "NONE"})
        for c in result["aufrufe"]:
            self.t.call(c["werkzeug"], c["argumente"], {"id": "x"})
        done = self.plan()
        self.assertEqual(done["status"], "fertig")
        self.assertIn("1 neu, 1 geändert, 1 gestrichen", done["meldung"])

    def test_own_event_in_the_slot_blocks_the_copy(self):
        own = {"id": "own", "summary": "Vorlesung B", "status": "confirmed",
               "start": {"dateTime": "2026-11-03T12:30:00+01:00"}, "end": {"dateTime": "2026-11-03T15:45:00+01:00"}}
        self.t.call("list_events", {"calendarId": TARGET, "startTime": "2026-11-03T12:30:00+01:00",
                                    "endTime": "2026-11-03T15:45:00+01:00", "pageSize": 250}, {"events": [own]})
        result = self.plan()
        self.assertNotIn("create_event", [c["werkzeug"] for c in result["aufrufe"]])
        self.assertTrue(any("schon in Ginas Kalender" in n for n in result["hinweise"]))

    def test_failed_write_stops_the_run(self):
        self.t.call("list_events", {"calendarId": TARGET, "startTime": "2026-11-03T12:30:00+01:00",
                                    "endTime": "2026-11-03T15:45:00+01:00", "pageSize": 250}, {"events": []})
        for c in self.plan()["aufrufe"]:
            self.t.call(c["werkzeug"], c["argumente"], {"id": "x"}, error=c["werkzeug"] == "delete_event")
        result = self.plan()
        self.assertEqual(result["status"], "fehler")
        self.assertIn("fehlgeschlagen", result["meldung"])


class Guards(Base):
    def test_many_missing_events_change_nothing(self):
        copies = [copy("Gross B", f"k{i}@ehb", f"K{i}", f"2026-11-1{i}T08:30:00+01:00", f"2026-11-1{i}T09:00:00+01:00") for i in range(5)]
        self.listed([LECTURE_COPY, *copies])
        result = self.plan()
        self.assertEqual(result["status"], "fertig")
        self.assertIn("An dieser Quelle ändert der Abgleich nichts", result["meldung"])

    def test_empty_feed_changes_nothing(self):
        self.gross.write_text(ics(), encoding="utf-8")
        self.listed([LECTURE_COPY], joseph=[{"id": "j1", "summary": "Klinikeinsatz", "status": "confirmed",
                                             "start": {"date": "2026-11-02T00:00:00Z"}, "end": {"date": "2026-11-09T00:00:00Z"}}])
        self.t.call("list_events", {"calendarId": TARGET, "startTime": "2026-11-02T00:00:00+01:00",
                                    "endTime": "2026-11-09T00:00:00+01:00", "pageSize": 250}, {"events": []})
        result = self.plan()
        self.assertEqual([c["werkzeug"] for c in result["aufrufe"]], ["create_event"])
        self.assertTrue(result["aufrufe"][0]["argumente"]["allDay"])
        self.assertTrue(any(n.startswith("Gross B: Die Quelle ist leer") for n in result["hinweise"]))

    def test_no_copies_found_is_an_error(self):
        self.listed([])
        result = self.plan()
        self.assertEqual(result["status"], "fehler")

    def test_same_event_in_two_feeds_comes_once(self):
        self.klein.write_text(ics(vevent("z@ehb", "Vorlesung A", "20261102T083000", "20261102T091500")), encoding="utf-8")
        self.listed([LECTURE_COPY])
        result = self.plan()
        self.assertEqual(result["status"], "lesen")
        self.assertEqual(len(result["aufrufe"]), 1)


class Parsing(unittest.TestCase):
    def test_folding_escapes_and_times(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f.ics"
            path.write_text("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Lange\r\n  Zeile\\, mit Komma\r\n"
                            "DTSTART:20261102T073000Z\r\nDTEND:20261102T083000Z\r\nDESCRIPTION:a\\nb\r\nEND:VEVENT\r\n"
                            "BEGIN:VEVENT\r\nUID:u2\r\nSUMMARY:Ganztag\r\nDTSTART;VALUE=DATE:20261103\r\nEND:VEVENT\r\n"
                            "BEGIN:VEVENT\r\nUID:u3\r\nSUMMARY:Serie\r\nRRULE:FREQ=WEEKLY\r\nDTSTART:20261104T080000Z\r\nEND:VEVENT\r\n"
                            "END:VCALENDAR\r\n", encoding="utf-8")
            notes = []
            events = ab.ics_events("Gross B", path, notes)
        self.assertEqual(events[0]["summary"], "Lange Zeile, mit Komma")
        self.assertEqual(events[0]["description"], "a\nb")
        self.assertEqual(events[0]["start"].isoformat(), "2026-11-02T08:30:00+01:00")
        self.assertEqual((events[1]["start"], events[1]["end"]), (date(2026, 11, 3), date(2026, 11, 4)))
        self.assertEqual(len(events), 2)
        self.assertTrue(notes and "Serie" in notes[0])


if __name__ == "__main__":
    unittest.main()
