# Hala Centralna Bowling Food Sniper

Monitors bowling lane availability on kregielnia24.pl and reports newly
available configured slots to the console and/or Pushbullet.

## Quick start

Requires [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp local-config.example.yaml local-config.yaml
uv run hcbowlingfoodsniper --once
```

For continuous monitoring, omit `--once`:

```bash
uv run hcbowlingfoodsniper
```

To enable Pushbullet, add an access token under
`notifications.pushbullet.access_token` or set `PUSHBULLET_ACCESS_TOKEN`, then
include `pushbullet` under `notifications.providers`. The local configuration
file is ignored by Git.

## Configuration

`monitor.checks` is a list of dates and optional exact start times. Use
`times: []` to monitor every available time for that date:

```yaml
monitor:
  checks:
    - date: "2026-11-10"
      times: ["15:00", "18:00"]
    - date: "2026-11-11"
      times: []
  interval_seconds: 300
```

Dates use `YYYY-MM-DD`; times use `HH:MM`. Add one list item for each date.
Notifications include the date-specific booking link and bowling alley phone
number. The monitor remembers reported date/time/lane combinations in
`data/seen-slots.json`, so the same slot is not reported repeatedly.

The script reads availability from the booking page's HTML; it does not submit
or create reservations.
