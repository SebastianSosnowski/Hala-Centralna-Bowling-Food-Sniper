import argparse
import datetime
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import yaml
from bs4 import BeautifulSoup
from pushbullet.errors import PushbulletError

LOGGER = logging.getLogger("hcbowlingfoodsniper")
DEFAULT_CONFIG = "local-config.yaml"
BOOKING_URL = "https://kregielnia24.pl/rezerwacja"
BOOKING_ID = 423
BOOKING_SEONAME = "bowling"
BOOKING_PHONE = "608 466 541"
REQUEST_TIMEOUT_SECONDS = 20
STATE_FILE = Path("./data/seen-slots.json")
SLOT_PATTERN = re.compile(r"e\[(\d+),(\d{1,2}_\d{1,2}_\d{4}_\d{2}_\d{2}_\d{2})\]")
RESERVED_PATTERN = re.compile(r"(\d+)#(\d{1,2}_\d{1,2}_\d{4}_\d{2}_\d{2}_\d{2})")


def parse_date(value: Any, field: str) -> datetime.date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a quoted YYYY-MM-DD date.")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date.") from error


def parse_checks(checks: Any) -> list[tuple[datetime.date, set[str] | None]]:
    if not isinstance(checks, list) or not checks:
        raise ValueError("monitor.checks must be a non-empty list.")

    parsed: list[tuple[datetime.date, set[str] | None]] = []
    seen_dates: set[datetime.date] = set()
    for index, check in enumerate(checks):
        field = f"monitor.checks[{index}]"
        if not isinstance(check, dict):
            raise ValueError(f"{field} must be a mapping with date and times.")
        day = parse_date(check.get("date"), f"{field}.date")
        if day in seen_dates:
            raise ValueError(f"Duplicate date in monitor.checks: {day.isoformat()}.")
        seen_dates.add(day)

        times = check.get("times")
        if not isinstance(times, list) or not all(isinstance(item, str) for item in times):
            raise ValueError(f"{field}.times must be a list of quoted HH:MM times.")
        for value in times:
            if not re.fullmatch(r"\d{2}:\d{2}", value):
                raise ValueError(f"Invalid time {value!r} in {field}.times; use HH:MM.")
            try:
                datetime.time.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"Invalid time {value!r} in {field}.times.") from error
        parsed.append((day, set(times) if times else None))
    return parsed


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).expanduser().open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("Configuration must contain a YAML mapping.")

    monitor = config.get("monitor")
    notifications = config.get("notifications")
    if not isinstance(monitor, dict) or not isinstance(notifications, dict):
        raise ValueError("Configuration must define 'monitor' and 'notifications' sections.")
    parse_checks(monitor.get("checks"))

    interval = monitor.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        raise ValueError("monitor.interval_seconds must be a positive integer.")

    providers = notifications.get("providers")
    if not isinstance(providers, list) or not providers or not all(
        provider in ("console", "pushbullet") for provider in providers
    ):
        raise ValueError("notifications.providers must contain 'console' and/or 'pushbullet'.")
    if len(set(providers)) != len(providers):
        raise ValueError("notifications.providers must not contain duplicates.")

    pushbullet = notifications.get("pushbullet", {})
    if not isinstance(pushbullet, dict):
        raise ValueError("notifications.pushbullet must be a mapping.")
    token = os.environ.get("PUSHBULLET_ACCESS_TOKEN") or pushbullet.get("access_token")
    if "pushbullet" in providers and not token:
        raise ValueError(
            "Set notifications.pushbullet.access_token or PUSHBULLET_ACCESS_TOKEN."
        )
    pushbullet["access_token"] = token
    notifications["pushbullet"] = pushbullet
    return config


def booking_url(day: datetime.date) -> str:
    return f"{BOOKING_URL}?{urlencode({
        'id': BOOKING_ID,
        'seoname': BOOKING_SEONAME,
        'day': day.day,
        'month': day.month,
        'year': day.year,
    })}"


def fetch_available_slots(
    day: datetime.date,
    session: requests.Session,
) -> list[tuple[datetime.date, str, str]]:
    response = session.get(
        BOOKING_URL,
        params={
            "id": BOOKING_ID,
            "seoname": BOOKING_SEONAME,
            "day": day.day,
            "month": day.month,
            "year": day.year,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return parse_available_slots(response.text, day)


def parse_available_slots(
    html: str,
    day: datetime.date,
) -> list[tuple[datetime.date, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    selected_day = soup.select_one(".zmdi-n-1-square-day")
    rows = soup.find(id="RowsRender")
    reserved_rows = soup.find(id="ReservedRows")
    if selected_day is None or rows is None or reserved_rows is None:
        raise ValueError(
            f"Response for {day.isoformat()} is missing its selected date or "
            "availability fields (RowsRender/ReservedRows)."
        )
    selected_date = selected_day.get_text(strip=True).split(maxsplit=1)[0]
    if selected_date != day.strftime("%d.%m.%Y"):
        raise ValueError(
            f"Requested {day.isoformat()} but the response shows {selected_date}."
        )

    rows_value = rows.get("value")
    reserved_value = reserved_rows.get("value")
    if not isinstance(rows_value, str) or not isinstance(reserved_value, str):
        raise ValueError(f"Availability fields for {day.isoformat()} have no value.")

    all_slots = set(SLOT_PATTERN.findall(rows_value))
    reserved_slots = set(RESERVED_PATTERN.findall(reserved_value))
    lanes = {
        element.get("data-header-tor-id"): element.get_text(strip=True)
        for element in soup.select("[data-header-tor-id]")
        if element.get("data-header-tor-id")
    }

    available: list[tuple[datetime.date, str, str]] = []
    for lane_id, timestamp in sorted(all_slots - reserved_slots):
        date_text, hour, minute, _second = timestamp.rsplit("_", 3)
        slot_day = datetime.datetime.strptime(date_text, "%d_%m_%Y").date()
        if slot_day != day:
            continue
        available.append((slot_day, f"{hour}:{minute}", lanes.get(lane_id, lane_id)))
    return available


def filter_slots(
    slots: list[tuple[datetime.date, str, str]],
    checks: list[tuple[datetime.date, set[str] | None]],
) -> list[tuple[datetime.date, str, str]]:
    times_by_date = dict(checks)
    return [
        slot
        for slot in slots
        if slot[0] in times_by_date
        and (times_by_date[slot[0]] is None or slot[1] in times_by_date[slot[0]])
    ]


def format_notification(slots: list[tuple[datetime.date, str, str]]) -> str:
    grouped: dict[datetime.date, dict[str, list[str]]] = {}
    for day, slot_time, lane in slots:
        grouped.setdefault(day, {}).setdefault(slot_time, []).append(lane)

    lines = []
    for day in sorted(grouped):
        lines.append(f"{day.strftime('%d.%m.%Y')}:")
        for slot_time in sorted(grouped[day]):
            lanes = ", ".join(sorted(grouped[day][slot_time]))
            lines.append(f"  {slot_time} — tory: {lanes}")
        lines.append(f"  Rezerwacja: {booking_url(day)}")
    lines.extend(["", f"Telefon do kręgielni: {BOOKING_PHONE}"])
    return "\n".join(lines)


def load_seen_slots(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open(encoding="utf-8") as state_file:
            lines = state_file.read().splitlines()
    except OSError as error:
        raise ValueError(f"Could not read state file {path}: {error}") from error
    return set(lines)


def save_seen_slots(path: Path, seen_slots: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        state_file.write("\n".join(sorted(seen_slots)))
        state_file.write("\n")
    temporary_path.replace(path)


def build_notifiers(config: dict[str, Any]) -> list[Callable[[str], None]]:
    notifications = config["notifications"]
    notifiers: list[Callable[[str], None]] = []
    if "console" in notifications["providers"]:
        notifiers.append(lambda message: print(message))
    if "pushbullet" in notifications["providers"]:
        from pushbullet import Pushbullet

        client = Pushbullet(notifications["pushbullet"]["access_token"])
        title = notifications["pushbullet"].get("title", "Wolny termin na kręgielni")
        notifiers.append(lambda message: client.push_note(title, message))
    return notifiers


def check_once(
    config: dict[str, Any],
    notifiers: list[Callable[[str], None]],
    session: requests.Session | None = None,
) -> None:
    checks = parse_checks(config["monitor"]["checks"])
    http = session or requests.Session()
    matched_slots: list[tuple[datetime.date, str, str]] = []
    for day, _times in checks:
        matched_slots.extend(fetch_available_slots(day, http))
    matched_slots = filter_slots(matched_slots, checks)
    if not matched_slots:
        LOGGER.info("No matching available slots found.")
        return

    state_path = STATE_FILE.expanduser()
    seen_slots = load_seen_slots(state_path)
    fresh_slots = [
        slot
        for slot in matched_slots
        if f"{slot[0].isoformat()}|{slot[1]}|{slot[2]}" not in seen_slots
    ]
    if not fresh_slots:
        LOGGER.info("Available slots found, but all were already reported.")
        return

    message = format_notification(fresh_slots)
    LOGGER.info("New available slots:\n%s", message)
    for notifier in notifiers:
        notifier(message)
    seen_slots.update(
        f"{day.isoformat()}|{slot_time}|{lane}" for day, slot_time, lane in fresh_slots
    )
    save_seen_slots(state_path, seen_slots)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor bowling lane availability.")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"YAML configuration file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once instead of polling continuously.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)
    notifiers = build_notifiers(config)
    interval = config["monitor"]["interval_seconds"]
    if args.once:
        STATE_FILE.expanduser().unlink(missing_ok=True)

    LOGGER.info("Bowling availability monitor started.")
    while True:
        try:
            check_once(config, notifiers)
        except (PushbulletError, requests.RequestException, OSError, ValueError):
            LOGGER.exception("Availability check failed.")
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
