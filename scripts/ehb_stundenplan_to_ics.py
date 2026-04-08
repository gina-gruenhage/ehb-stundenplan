#!/usr/bin/env python3
"""
EHB Stundenplan JSON → ICS-Feeds pro Gruppe.

Liest das von ehb_stundenplan_fetch.py erzeugte JSON und schreibt pro Gruppe
eine ICS-Datei. Zwei unabhaengige Einteilungen:

  - Grossgruppen A-D: enthalten Plenum + Events dieser Gruppe (1 Abo reicht)
  - Kleingruppen 1a-3b: nur die Events dieser Kleingruppe (zusaetzlich abonnieren)
  - plenum.ics: alle Events ohne Gruppen-Marker (Fallback, fuer Lehrende o.ae.)
  - full.ics: alle Events (Debug)

Event-UIDs sind stabil (Hash aus Datum+Zeit+Titel+Raum), damit Kalender-Clients
Updates korrekt zuordnen. DTSTAMP wird bei jedem Lauf aktualisiert, damit
abonnierende Clients Aenderungen erkennen.

Usage:
    python3 execution/ehb_stundenplan_to_ics.py
    python3 execution/ehb_stundenplan_to_ics.py --json tmp/ehb_events.json --out studium/EHB-Stundenplan/ics
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

DEFAULT_JSON = Path("tmp/ehb_events.json")
DEFAULT_OUT = Path("studium/EHB-Stundenplan/ics")
TZ = ZoneInfo("Europe/Berlin")

GROSS = ["A", "B", "C", "D"]
KLEIN = ["1a", "1b", "2a", "2b", "3a", "3b"]

UID_DOMAIN = "ehb-stundenplan.claudette"


def stable_uid(event: dict) -> str:
    key = f"{event['date']}|{event['start']}|{event['title']}|{event['room']}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{h}@{UID_DOMAIN}"


def to_ical_event(ev: dict, now_utc: datetime) -> Event:
    ical = Event()
    ical.add("uid", stable_uid(ev))
    ical.add("dtstamp", now_utc)

    dt_start = datetime.fromisoformat(f"{ev['date']}T{ev['start']}:00").replace(tzinfo=TZ)
    dt_end = datetime.fromisoformat(f"{ev['date']}T{ev['end']}:00").replace(tzinfo=TZ)
    ical.add("dtstart", dt_start)
    ical.add("dtend", dt_end)

    summary = ev["title"]
    if ev["groups"]:
        summary = f"[{'/'.join(ev['groups'])}] {summary}"
    ical.add("summary", summary)

    if ev["room"]:
        ical.add("location", ev["room"])

    desc_lines = []
    if ev["lecturer"]:
        desc_lines.append(f"Dozent: {ev['lecturer']}")
    if ev["beschreibung"]:
        desc_lines.append(ev["beschreibung"])
    if ev["modul_code"]:
        desc_lines.append(f"Modul: {ev['modul_code']}")
    if ev["groups"]:
        desc_lines.append(f"Gruppe: {', '.join(ev['groups'])}")
    if desc_lines:
        ical.add("description", "\n".join(desc_lines))

    return ical


def build_calendar(name: str, events: list[dict]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", f"-//Claudette//EHB Stundenplan {name}//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"EHB Hebammen {name}")
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("method", "PUBLISH")

    now_utc = datetime.now(ZoneInfo("UTC"))
    for ev in events:
        cal.add_component(to_ical_event(ev, now_utc))
    return cal


def filter_gross(events: list[dict], gross: str) -> list[dict]:
    """Plenum + Events dieser Grossgruppe. Kleingruppen-Events werden ausgeschlossen."""
    out = []
    for e in events:
        groups = e["groups"]
        if not groups:
            out.append(e)
        elif gross in groups:
            out.append(e)
    return out


def filter_klein(events: list[dict], klein: str) -> list[dict]:
    return [e for e in events if klein in e["groups"]]


def filter_plenum(events: list[dict]) -> list[dict]:
    return [e for e in events if not e["groups"]]


def write_ics(cal: Calendar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cal.to_ical())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", default=str(DEFAULT_JSON), type=Path)
    p.add_argument("--out", default=str(DEFAULT_OUT), type=Path)
    args = p.parse_args()

    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    events: list[dict] = data["events"]

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: list[tuple[str, int]] = []

    # Grossgruppen (Plenum + eigene Events)
    for g in GROSS:
        sel = filter_gross(events, g)
        name = f"Gross {g}"
        write_ics(build_calendar(name, sel), out_dir / f"gross-{g.lower()}.ics")
        summary.append((f"gross-{g.lower()}.ics", len(sel)))

    # Kleingruppen (nur eigene Events)
    for k in KLEIN:
        sel = filter_klein(events, k)
        name = f"Klein {k}"
        write_ics(build_calendar(name, sel), out_dir / f"klein-{k}.ics")
        summary.append((f"klein-{k}.ics", len(sel)))

    print(f"[ehb-ics] Ausgabe: {out_dir}", file=sys.stderr)
    for name, count in summary:
        print(f"  {name:20s} {count:4d} Events", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
