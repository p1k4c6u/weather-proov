"""Market discovery via Polymarket Gamma API.

For each station, enumerate target dates (today and next 3 local days) and
fetch the event at the corresponding slug. Each event contains sub-markets, one
per bucket. We parse those into the `markets` table.

Slugs observed in Polymarket weather markets typically follow:
    event:     highest-temperature-in-<city>-on-YYYY-MM-DD
    sub-market: highest-temperature-in-<city>-on-YYYY-MM-DD-be-<bucket>
                                                          ^^^^^^^^^^^^^
                                                          where <bucket> is e.g.:
                                                            "85"           band
                                                            "90-or-higher" open_high
                                                            "70-or-lower"  open_low

The exact slug format is verified at build-time by `wxm fetch markets` against
the real API; this parser is permissive about prefix stripping and tail-direction
keywords so small variations don't require code changes. Hand-crafted fixture
under tests/fixtures/gamma/ — update when real Gamma payloads land in
data/raw/markets/.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from ..archive import archive_raw
from ..db import DEFAULT_DB_PATH, connect
from ..spec import CitySpec, Spec

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

_OPEN_HIGH_SUFFIXES = ("-or-higher", "-or-above", "-or-more")
_OPEN_LOW_SUFFIXES = ("-or-lower", "-or-below", "-or-less")
_TRAILING_UNIT_RE = re.compile(r"-(?:c|f|celsius|fahrenheit|degrees?)$")
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class ParsedMarket:
    market_id: str
    condition_id: str
    station: str
    target_date: str
    bucket_label: float
    bucket_kind: str  # "band" | "open_low" | "open_high"
    token_id_yes: str
    token_id_no: str
    closed: bool


def _local_today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def target_dates_for_station(city: CitySpec, days_ahead: int = 4) -> list[str]:
    today = _local_today(city.timezone)
    return [
        (today + timedelta(days=k)).isoformat()
        for k in range(days_ahead)
    ]


def event_slug_for_date(city: CitySpec, target_date: str) -> str:
    return city.polymarket_slug_pattern.format(date=target_date)


def fetch_event(slug: str, client: httpx.Client) -> dict[str, Any] | None:
    """Fetch a single event by slug. Returns None on 404 or empty response.

    Raw response body is archived under data/raw/markets/ before parsing.
    """
    r = client.get(f"{GAMMA_BASE_URL}/events", params={"slug": slug}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    archive_raw("markets", r.content)
    data = r.json()
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        if "events" in data:
            evs = data["events"]
            return evs[0] if evs else None
        return data
    return None


def _coerce_token_ids(raw: Any) -> list[str]:
    """Polymarket Gamma encodes clobTokenIds as a JSON-encoded string OR a real list."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    return []


def parse_market_record(
    raw: dict[str, Any], station: str, expected_target_date: str | None = None
) -> ParsedMarket | None:
    """Parse a Gamma sub-market into a ParsedMarket. Returns None if unparseable.

    Expects the sub-market's slug to extend the event slug with the bucket portion.
    """
    slug = raw.get("slug", "")
    if not slug:
        return None

    # Find a YYYY-MM-DD in the slug, then take everything after it as the bucket portion.
    m = re.search(r"(\d{4}-\d{2}-\d{2})-be-(?P<bucket>.+)$", slug)
    if not m:
        m = re.search(r"(\d{4}-\d{2}-\d{2})-(?P<bucket>(?!.*\d{4}-\d{2}-\d{2}).+)$", slug)
    if not m:
        return None
    target_date = m.group(1)
    if expected_target_date is not None and target_date != expected_target_date:
        return None
    bucket_part = m.group("bucket")

    bucket_kind = "band"
    bucket_str = bucket_part
    for suf in _OPEN_HIGH_SUFFIXES:
        if bucket_part.endswith(suf):
            bucket_kind = "open_high"
            bucket_str = bucket_part[: -len(suf)]
            break
    else:
        for suf in _OPEN_LOW_SUFFIXES:
            if bucket_part.endswith(suf):
                bucket_kind = "open_low"
                bucket_str = bucket_part[: -len(suf)]
                break

    bucket_str = _TRAILING_UNIT_RE.sub("", bucket_str)
    if not _NUMERIC_RE.match(bucket_str):
        return None
    bucket_label = float(bucket_str)

    market_id = str(raw.get("id") or raw.get("conditionId") or "")
    condition_id = str(raw.get("conditionId", ""))
    tokens = _coerce_token_ids(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
    if len(tokens) < 2 or not market_id:
        return None

    return ParsedMarket(
        market_id=market_id,
        condition_id=condition_id,
        station=station,
        target_date=target_date,
        bucket_label=bucket_label,
        bucket_kind=bucket_kind,
        token_id_yes=tokens[0],
        token_id_no=tokens[1],
        closed=bool(raw.get("closed", False)),
    )


def parse_event(
    event: dict[str, Any], station: str, expected_target_date: str | None = None
) -> list[ParsedMarket]:
    markets = event.get("markets") or []
    out: list[ParsedMarket] = []
    for m in markets:
        parsed = parse_market_record(m, station, expected_target_date)
        if parsed is not None:
            out.append(parsed)
    return out


def upsert_markets(parsed: Iterable[ParsedMarket], db_path: Path = DEFAULT_DB_PATH) -> int:
    parsed_list = list(parsed)
    if not parsed_list:
        return 0
    conn = connect(db_path)
    try:
        now = int(time.time())
        for p in parsed_list:
            conn.execute(
                "INSERT INTO markets("
                "  market_id, station, date, bucket_label, bucket_kind,"
                "  token_id_yes, token_id_no, discovered_ts, closed) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(market_id) DO UPDATE SET "
                "  station=excluded.station, date=excluded.date,"
                "  bucket_label=excluded.bucket_label, bucket_kind=excluded.bucket_kind,"
                "  token_id_yes=excluded.token_id_yes, token_id_no=excluded.token_id_no,"
                "  closed=excluded.closed",
                (
                    p.market_id,
                    p.station,
                    p.target_date,
                    p.bucket_label,
                    p.bucket_kind,
                    p.token_id_yes,
                    p.token_id_no,
                    now,
                    int(p.closed),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return len(parsed_list)


def discover_markets(
    spec: Spec,
    db_path: Path = DEFAULT_DB_PATH,
    days_ahead: int = 4,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    """For each station, fetch events for the next `days_ahead` local dates, parse,
    persist into the markets table. Returns {station_id: rows_upserted}.
    """
    counts: dict[str, int] = {}
    owned_client = client is None
    if owned_client:
        client = httpx.Client()
    try:
        for station_id, city in spec.resolution.cities.items():
            station_total = 0
            for target_date in target_dates_for_station(city, days_ahead):
                slug = event_slug_for_date(city, target_date)
                event = fetch_event(slug, client)
                if event is None:
                    continue
                parsed = parse_event(event, station_id, expected_target_date=target_date)
                station_total += upsert_markets(parsed, db_path)
            counts[station_id] = station_total
    finally:
        if owned_client:
            client.close()
    return counts
