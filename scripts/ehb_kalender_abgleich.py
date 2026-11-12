#!/usr/bin/env python3
"""Gleicht Ginas Kalender mit den EHB-Quellen ab. Kern der Routine routine/kalender-abgleich.md.

Quellen sind die Feeds Klein 1b und Gross B unter docs/ics/ und der Google-Kalender
„EHB St. Joseph Termine". Jeder übernommene Termin hat als letzte Zeile der Beschreibung
„EHB-Abgleich: <Quelle> (<Schlüssel>)". Nur solche Termine legt der Abgleich an, ändert
oder löscht er. Ginas eigene Termine bleiben unberührt, vergangene Tage auch.

Der Lauf erreicht den Kalender nur über den Connector. `plan` liest dessen Antworten aus
dem Transkript der laufenden Session und nennt die nächsten Aufrufe. Der Agent führt sie
aus und ruft `plan` erneut, bis `fertig` oder `fehler` kommt.

Aufruf:
    python3 scripts/ehb_kalender_abgleich.py plan
    python3 scripts/ehb_kalender_abgleich.py plan --session PFAD.jsonl --calendar ID --joseph-calendar ID
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
REPO = Path(__file__).resolve().parent.parent
FEEDS = [("Klein 1b", REPO / "docs/ics/klein-1b.ics"), ("Gross B", REPO / "docs/ics/gross-b.ics")]
JOSEPH = "St. Joseph"
JOSEPH_NAME = re.compile(r"st\.?\s*joseph", re.IGNORECASE)
SOURCES = [label for label, _ in FEEDS] + [JOSEPH]
FOOTER_RE = re.compile(r"\s*EHB-Abgleich: (Klein 1b|Gross B|St\. Joseph) \(([^()\s]+)\)\s*$")
SEARCH = "EHB-Abgleich"
# Ohne Endzeit liefert der Connector nur ein kurzes Zeitfenster (gemessen 01.10.2026: 2 von 54 Terminen).
HORIZON = timedelta(days=400)
# Fehlt mehr als ein Viertel der künftigen Termine einer Quelle, ist eher die Quelle kaputt als der Plan neu.
MAX_REMOVE_SHARE = 0.25
OPS = {"list_calendars", "list_events", "create_event", "update_event", "delete_event"}
WRITES = ("create_event", "update_event", "delete_event")


# --- Transkript --------------------------------------------------------------

def transcript(arg: str | None) -> Path | None:
    if arg:
        return Path(arg) if Path(arg).is_file() else None
    root = Path.home() / ".claude" / "projects"
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", session):
        hits = sorted(root.glob(f"**/{session}.jsonl"))
        if hits:
            return hits[0]
    files = [p for p in root.glob("**/*.jsonl") if p.stat().st_size > 0] if root.is_dir() else []
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def operation(name: str) -> str | None:
    """`list_events` aus `mcp__Google-Calendar__list_events` oder `mcp__claude_ai_Google_Calendar__list_events`."""
    norm = name.lower().replace("-", "_")
    op = norm.rsplit("__", 1)[-1]
    return op if op in OPS and "calendar" in norm else None


def result_of(entry: dict, block: dict):
    structured = (entry.get("mcpMeta") or {}).get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = block.get("content")
    texts = [content] if isinstance(content, str) else [c.get("text", "") for c in content or [] if isinstance(c, dict)]
    for text in texts:
        # Ein zu großes Ergebnis legt die Laufzeit als Datei ab und nennt nur den Pfad.
        moved = re.match(r"\s*Error: result \([\d,. ]+ characters\) exceeds maximum allowed tokens\.\s+"
                         r"Output has been saved to (/\S+?)\.\s", text)
        if moved and "tool-results" in Path(moved.group(1)).parts and Path(moved.group(1)).is_file():
            text = Path(moved.group(1)).read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def read_calls(path: Path) -> list[dict]:
    """Alle Griffe auf den Kalender-Connector in Aufrufreihenfolge, mit Ergebnis oder Fehler."""
    calls, by_id = [], {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (entry.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and operation(str(block.get("name", ""))):
                    call = {"op": operation(block["name"]), "input": block.get("input") or {}, "done": False,
                            "error": False, "result": None}
                    calls.append(call)
                    by_id[block.get("id")] = call
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                    call = by_id[block["tool_use_id"]]
                    call["done"], call["error"] = True, bool(block.get("is_error"))
                    if not call["error"]:
                        call["result"] = result_of(entry, block)
    return calls


# --- Termine -----------------------------------------------------------------

def unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def ics_time(params: str, value: str):
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=timezone.utc).astimezone(TZ)
    zone = re.search(r"TZID=([^;:]+)", params)
    return moment.replace(tzinfo=ZoneInfo(zone.group(1)) if zone else TZ)


def ics_events(source: str, path: Path, notes: list[str]) -> list[dict]:
    out, cur = [], None
    for line in unfold(path.read_text(encoding="utf-8")):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("RRULE"):
                notes.append(f"{source}: Serie „{cur.get('SUMMARY', '')}“ in der Quelle. Serien übernimmt der Abgleich nicht.")
            elif cur.get("UID") and cur.get("DTSTART") and cur.get("STATUS", "").upper() != "CANCELLED":
                start = cur["DTSTART"]
                end = cur.get("DTEND") or (start + timedelta(days=1) if type(start) is date else start)
                out.append(event(source, cur["UID"], cur.get("SUMMARY", ""), cur.get("DESCRIPTION", ""),
                                 cur.get("LOCATION", ""), start, end))
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            if name in ("DTSTART", "DTEND"):
                cur[name] = ics_time(params, value)
            elif name in ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "RRULE", "STATUS"):
                cur[name] = unescape(value)
    return out


def mcp_time(value: dict):
    if value.get("date"):
        return date.fromisoformat(value["date"][:10])
    return datetime.fromisoformat(value["dateTime"]).astimezone(TZ)


def event(source, key, summary, description, location, start, end, event_id=None) -> dict:
    return {"source": source, "key": key, "id": event_id, "summary": (summary or "").strip(),
            "description": (description or "").replace("\r\n", "\n").strip(), "location": (location or "").strip(),
            "start": start, "end": end}


def from_listing(item: dict, source: str | None = None) -> dict | None:
    """Ein Termin aus einer list_events-Antwort. Ohne `source` nur, wenn er die Abgleich-Zeile hat."""
    description = item.get("description") or ""
    found = FOOTER_RE.search(description)
    if source is None:
        if not found:
            return None
        source, key = found.group(1), found.group(2)
    else:
        key = item.get("id", "")
    if found:
        description = description[:found.start()]
    return event(source, key, item.get("summary"), description, item.get("location"),
                 mcp_time(item["start"]), mcp_time(item["end"]), item.get("id"))


def all_day(e: dict) -> bool:
    return type(e["start"]) is date


def stamp(value) -> str:
    return value.isoformat() if type(value) is date else value.astimezone(timezone.utc).isoformat()


def slot(e: dict) -> tuple:
    return stamp(e["start"]), stamp(e["end"])


def ends_after(e: dict, moment: datetime) -> bool:
    end = e["end"]
    return (datetime.combine(end, time(0), TZ) if type(end) is date else end) > moment


def in_span(e: dict, day_start: datetime) -> bool:
    start = e["start"]
    start = datetime.combine(start, time(0), TZ) if type(start) is date else start
    return ends_after(e, day_start) and start < day_start + HORIZON


def same(a: dict, b: dict) -> bool:
    return all(a[f] == b[f] for f in ("summary", "description", "location")) and slot(a) == slot(b)


def footer(e: dict) -> str:
    return f"EHB-Abgleich: {e['source']} ({e['key']})"


def times(e: dict) -> dict:
    if all_day(e):
        return {"allDay": True, "startTime": f"{e['start']}T00:00:00", "endTime": f"{e['end']}T00:00:00",
                "timeZone": "Europe/Berlin"}
    return {"startTime": e["start"].isoformat(), "endTime": e["end"].isoformat(), "timeZone": "Europe/Berlin"}


def window(e: dict) -> tuple[str, str]:
    if all_day(e):
        return (datetime.combine(e["start"], time(0), TZ).isoformat(), datetime.combine(e["end"], time(0), TZ).isoformat())
    return e["start"].isoformat(), e["end"].isoformat()


def describe(e: dict) -> str:
    when = e["start"].strftime("%d.%m.%Y") if all_day(e) else e["start"].strftime("%d.%m.%Y %H:%M")
    return f"{when} {e['summary']}"


# --- Plan --------------------------------------------------------------------

def chain(calls: list[dict], match: dict) -> tuple[list, dict | None]:
    """Die Einträge der jüngsten vollständigen Seitenkette zu `match`, oder der nächste fällige Aufruf."""
    def fits(c):
        return c["op"] == "list_events" and all(c["input"].get(k) == v for k, v in match.items())
    firsts = [i for i, c in enumerate(calls) if fits(c) and not c["input"].get("pageToken") and c["done"] and not c["error"]]
    if not firsts:
        return [], dict(match, pageSize=250)
    items, page = [], calls[firsts[-1]]["result"] or {}
    while True:
        items += page.get("events") or page.get("items") or []
        token = page.get("nextPageToken")
        if not token:
            return items, None
        nxt = [c for c in calls[firsts[-1]:] if fits(c) and c["input"].get("pageToken") == token and c["done"] and not c["error"]]
        if not nxt:
            return items, dict(match, pageSize=250, pageToken=token)
        page = nxt[-1]["result"] or {}


def joseph_calendar(calls: list[dict]) -> str | None:
    lists = [c for c in calls if c["op"] == "list_calendars" and c["done"] and not c["error"]]
    for cal in ((lists[-1]["result"] or {}).get("calendars") or [] if lists else []):
        if JOSEPH_NAME.search(cal.get("summary") or ""):
            return cal.get("id")
    return None


def plan(calls: list[dict], today: date, target: str, joseph_id: str | None) -> dict:
    notes: list[str] = []
    day_start = datetime.combine(today, time(0), TZ)
    span = {"startTime": day_start.isoformat(), "endTime": (day_start + HORIZON).isoformat()}
    if joseph_id is None:
        if not any(c["op"] == "list_calendars" and c["done"] and not c["error"] for c in calls):
            return {"status": "lesen", "aufrufe": [{"werkzeug": "list_calendars", "argumente": {"pageSize": 250}}]}
        joseph_id = joseph_calendar(calls)
    reads, wanted = [], []
    managed_items, nxt = chain(calls, {"calendarId": target, "fullText": SEARCH, **span})
    if nxt:
        reads.append(nxt)
    for source, path in FEEDS:
        if not path.is_file():
            notes.append(f"{source}: Die Quelle ist nicht lesbar. Nichts geändert.")
            continue
        wanted += [e for e in ics_events(source, path, notes) if in_span(e, day_start)]
    if joseph_id:
        joseph_items, nxt = chain(calls, {"calendarId": joseph_id, **span})
        if nxt:
            reads.append(nxt)
        for item in joseph_items:
            if item.get("status") == "cancelled":
                continue
            if item.get("recurrence") or item.get("recurringEventId"):
                notes.append(f"{JOSEPH}: Serie „{item.get('summary', '')}“ in der Quelle. Serien übernimmt der Abgleich nicht.")
                continue
            e = from_listing(item, JOSEPH)
            if in_span(e, day_start):
                wanted.append(e)
    else:
        notes.append(f"{JOSEPH}: Den Kalender „EHB St. Joseph Termine“ gibt es in der Kalenderliste nicht. Nichts geändert.")
    if reads:
        return {"status": "lesen", "aufrufe": [{"werkzeug": "list_events", "argumente": a} for a in reads]}

    managed = [e for e in (from_listing(i) for i in managed_items if i.get("status") != "cancelled") if e and in_span(e, day_start)]
    readable = {e["source"] for e in wanted}
    for source in SOURCES:
        if source not in readable and not any(n.startswith(source + ":") for n in notes):
            notes.append(f"{source}: Die Quelle ist leer. Nichts geändert.")
    if not managed and wanted:
        return {"status": "fehler", "aufrufe": [], "meldung": "EHB-Abgleich: Die Suche nach „EHB-Abgleich“ findet in Ginas "
                "Kalender keinen einzigen übernommenen Termin. Ohne diese Termine würde der Abgleich alles doppelt anlegen. Nichts geändert."}

    # Derselbe Termin in zwei Quellen kommt nur einmal in den Kalender. Die Reihenfolge in SOURCES entscheidet.
    kept, seen = [], set()
    for e in sorted(wanted, key=lambda e: SOURCES.index(e["source"])):
        k = slot(e) + (e["summary"].lower(),)
        if k not in seen:
            seen.add(k)
            kept.append(e)

    creates, updates, deletes = [], [], []
    for source in sorted(readable):
        want = {e["key"]: e for e in kept if e["source"] == source}
        have: dict[str, dict] = {}
        extra = []
        for e in (m for m in managed if m["source"] == source):
            if e["key"] in have:
                extra.append(e)
            else:
                have[e["key"]] = e
        gone = [e for k, e in have.items() if k not in want] + extra
        mine = len(have) + len(extra)
        if len(gone) > max(2, mine * MAX_REMOVE_SHARE):
            notes.append(f"{source}: {len(gone)} künftige Termine fehlen in der Quelle. Das sieht nach einem Fehler der Quelle aus. "
                         f"An dieser Quelle ändert der Abgleich nichts.")
            continue
        creates += [e for k, e in want.items() if k not in have]
        updates += [(have[k], e) for k, e in want.items() if k in have and not same(have[k], e)]
        deletes += gone

    checks = []
    for e in creates:
        start, end = window(e)
        found = [c for c in calls if c["op"] == "list_events" and c["done"] and not c["error"]
                 and c["input"].get("calendarId") == target and c["input"].get("startTime") == start
                 and c["input"].get("endTime") == end and not c["input"].get("fullText")]
        if not found:
            checks.append({"calendarId": target, "startTime": start, "endTime": end, "pageSize": 250})
            continue
        e["_slot"] = (found[-1]["result"] or {}).get("events") or []
    if checks:
        return {"status": "lesen", "aufrufe": [{"werkzeug": "list_events", "argumente": a} for a in checks]}

    fresh = []
    for e in creates:
        if any(blocks(item, e) for item in e.pop("_slot")):
            notes.append(f"{e['source']}: {describe(e)} steht schon in Ginas Kalender. Nicht doppelt angelegt.")
        else:
            fresh.append(e)

    todo = [("create_event", e, None) for e in fresh] + [("update_event", n, o) for o, n in updates] + [("delete_event", o, o) for o in deletes]
    return finish(calls, todo, notes, target)


def blocks(item: dict, e: dict) -> bool:
    """Ginas eigener Termin zur selben Zeit oder die schon vorhandene Kopie verhindert das Anlegen."""
    if item.get("status") == "cancelled" or slot(from_listing(item, "")) != slot(e):
        return False
    tag = FOOTER_RE.search(item.get("description") or "")
    return tag is None or (tag.group(1), tag.group(2)) == (e["source"], e["key"])


def call_for(op: str, e: dict, old: dict | None, target: str) -> dict:
    if op == "delete_event":
        return {"calendarId": target, "eventId": old["id"], "notificationLevel": "NONE"}
    text = (e["description"] + "\n\n" if e["description"] else "") + footer(e)
    args = {"calendarId": target, "summary": e["summary"], "description": text, **times(e), "notificationLevel": "NONE"}
    if e["location"]:
        args["location"] = e["location"]
    if op == "create_event":
        args["useDefaultReminders"] = False
    else:
        args["eventId"] = old["id"]
    return args


def finish(calls: list[dict], todo: list, notes: list[str], target: str) -> dict:
    """Zieht ab, was der Lauf schon geschrieben hat. Danach bleibt der Rest, ein Fehler oder `fertig`."""
    done = {op: {} for op in WRITES}
    for c in calls:
        if c["op"] in WRITES and c["done"]:
            ref = c["input"].get("eventId") or FOOTER_RE.search(c["input"].get("description") or "")
            ref = ref.group(2) if isinstance(ref, re.Match) else ref
            if ref:
                done[c["op"]][ref] = c
    open_calls, failed, written = [], [], []
    for op, e, old in todo:
        ref = e["key"] if op == "create_event" else old["id"]
        c = done[op].get(ref)
        if c is None:
            open_calls.append({"werkzeug": op, "argumente": call_for(op, e, old, target)})
        elif c["error"]:
            failed.append(f"{e['source']}, {describe(e)}: {op} ist fehlgeschlagen.")
        else:
            written.append((op, e))
    if failed:
        return {"status": "fehler", "aufrufe": [], "meldung": report(written, notes + failed, "fehler")}
    if open_calls:
        return {"status": "schreiben", "aufrufe": open_calls, "hinweise": notes}
    return {"status": "fertig", "aufrufe": [], "meldung": report(written, notes, "fertig")}


def report(written: list, notes: list[str], status: str) -> str:
    words = {"create_event": "neu", "update_event": "geändert", "delete_event": "gestrichen"}
    counts = ", ".join(f"{sum(1 for op, _ in written if op == k)} {w}" for k, w in words.items())
    lines = [f"EHB-Abgleich {datetime.now(TZ):%d.%m.%Y}: {counts}." + (" Abgebrochen." if status == "fehler" else "")]
    lines += [f"{e['source']}, {words[op]}: {describe(e)}" for op, e in written]
    lines += notes
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="EHB-Termine in Ginas Kalender abgleichen.")
    parser.add_argument("command", choices=["plan"])
    parser.add_argument("--session", help="Transkript der Session (Standard: die laufende)")
    parser.add_argument("--calendar", default="primary", help="Zielkalender (Standard: primary)")
    parser.add_argument("--joseph-calendar", help="ID des St.-Joseph-Kalenders statt der Suche in der Kalenderliste")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    path = transcript(args.session)
    if path is None:
        print("Kein Session-Transkript gefunden. Mit --session angeben.", file=sys.stderr)
        return 1
    result = plan(read_calls(path), args.today or datetime.now(TZ).date(), args.calendar, args.joseph_calendar)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
