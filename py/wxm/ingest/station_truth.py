"""Settlement-grade daily-max observations.

Three sources, one settlement role per station:

  - HKO (Hong Kong Observatory): monthly XML/CSV daily extract — settlement source
  - Wunderground daily JSON: KNYC + EGLC — settlement source for those stations
  - NWS CLI text product: KNYC cross-check only (is_settlement_source=0)

Phase 0 disclaimer: the exact upstream payload shapes vary by provider release
and product. The parsers here implement plausible, defensive parses keyed off the
known landmarks ("Maximum temperature", member node names, etc.). On real-world
ingestion, the raw payload is archived under data/raw/truth/{source}/ BEFORE
parsing so any parser bug is replayable; update the parser + fixture together.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..archive import archive_raw
from ..db import DEFAULT_DB_PATH, connect
from ..spec import Spec
from ..units import f_to_c

WU_HISTORICAL_URL = "https://api.weather.com/v1/location/{station}/observations/historical.json"
WU_PAGE_URL = "https://www.wunderground.com/history/daily/{path}/{station}/date/{date}"
NWS_PRODUCTS_URL = "https://api.weather.gov/products"
HKO_MONTHLY_XML_URL = "https://www.hko.gov.hk/cis/dailyExtract/dailyExtract_{yyyymm}.xml"


@dataclass(frozen=True)
class DailyMaxObservation:
    station: str
    date: str
    source: str
    value: float
    units: str  # "celsius" | "fahrenheit"
    is_settlement_source: bool


# ----------------------------------------------------------------------- HKO


_HKO_DAILY_MAX_RE = re.compile(r"\bdaily[\s_]*max", re.IGNORECASE)


def parse_hko_monthly_xml(xml_text: str, target_date: str) -> float | None:
    """Find the absolute daily maximum on target_date (YYYY-MM-DD) in °C.

    Tolerates several plausible tag layouts:
      <Day date="..."><DailyMax>28.5</DailyMax></Day>
      <day><date>...</date><dailyMax>28.5</dailyMax></day>
      <observation date="..." daily_max="28.5"/>
    The shared landmark is a node whose tag matches /daily.?max/ near the date.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    yyyy, mm, dd = target_date.split("-")
    day_num = str(int(dd))

    for node in root.iter():
        attrs = {k.lower(): v for k, v in node.attrib.items()}
        date_attr = attrs.get("date") or attrs.get("day") or ""
        if (
            date_attr == target_date
            or date_attr == day_num
            or date_attr == dd
        ):
            for child in node.iter():
                if _HKO_DAILY_MAX_RE.search(child.tag):
                    text = (child.text or "").strip()
                    try:
                        return float(text)
                    except ValueError:
                        continue
            for k, v in attrs.items():
                if _HKO_DAILY_MAX_RE.search(k):
                    try:
                        return float(v)
                    except ValueError:
                        continue
    return None


def fetch_hko(target_date: str, client: httpx.Client) -> str | None:
    yyyymm = target_date[:7].replace("-", "")
    r = client.get(HKO_MONTHLY_XML_URL.format(yyyymm=yyyymm), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    archive_raw("truth/hko", r.content)
    return r.text


# ---------------------------------------------------------------- Wunderground


_WU_APIKEY_RE = re.compile(r"\bapiKey['\"]?\s*[:=]\s*['\"]([0-9a-zA-Z]{20,})['\"]")


def extract_wu_api_key(page_html: str) -> str | None:
    m = _WU_APIKEY_RE.search(page_html)
    return m.group(1) if m else None


def parse_wu_historical_json(
    payload: dict, units_hint: str
) -> tuple[float, str] | None:
    """Compute the daily max from the observations array.

    Returns (max_value, units) where units is 'celsius' or 'fahrenheit' matching
    the API call (units=m → celsius; units=e → fahrenheit).
    """
    observations = payload.get("observations") or []
    if not observations:
        return None
    temps: list[float] = []
    for obs in observations:
        t = obs.get("temp")
        if t is None:
            continue
        try:
            temps.append(float(t))
        except (TypeError, ValueError):
            continue
    if not temps:
        return None
    units = "celsius" if units_hint == "m" else "fahrenheit"
    return max(temps), units


def fetch_wu(
    station_id: str, target_date: str, units_hint: str, api_key: str, client: httpx.Client
) -> dict | None:
    yyyymmdd = target_date.replace("-", "")
    url = WU_HISTORICAL_URL.format(station=station_id)
    r = client.get(
        url,
        params={"apiKey": api_key, "units": units_hint, "startDate": yyyymmdd},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    archive_raw("truth/wunderground", r.content)
    return r.json()


# ----------------------------------------------------------------------- NWS

_NWS_MAX_TEMP_RE = re.compile(
    r"MAXIMUM\s+TEMPERATURE.*?\b(-?\d{1,3})\b", re.IGNORECASE | re.DOTALL
)


def parse_nws_cli_text(text: str, target_date: str) -> float | None:
    """Extract the daily MAXIMUM TEMPERATURE from a CLI product.

    NWS CLI products are plain text climate reports. The simplest landmark is
    'MAXIMUM TEMPERATURE' followed (within the same product block) by an integer
    value in °F. This parser is intentionally simple; it expects the caller to
    have selected the CLI product whose climatology date matches target_date.
    """
    m = _NWS_MAX_TEMP_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def fetch_nws_cli(location: str, target_date: str, client: httpx.Client) -> str | None:
    """Find the most recent CLI product for ``location`` that covers target_date.

    Returns the product text, or None on miss. We list recent CLI products,
    download each in turn until one mentions ``target_date``.
    """
    r = client.get(
        NWS_PRODUCTS_URL,
        params={"type": "CLI", "location": location, "limit": 5},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    listing = r.json()
    archive_raw("truth/nws_cli_listing", r.content)
    products = (listing.get("@graph") or listing.get("products") or [])
    for p in products:
        pid = p.get("@id") or p.get("id")
        if not pid:
            continue
        # @id is a URL; otherwise build the URL
        url = pid if pid.startswith("http") else f"{NWS_PRODUCTS_URL}/{pid}"
        d = client.get(url, timeout=30)
        if d.status_code != 200:
            continue
        archive_raw("truth/nws_cli", d.content)
        text = (d.json().get("productText") or "")
        if target_date in text or target_date.replace("-", "") in text:
            return text
    return None


# ---------------------------------------------------------------- Persistence


def upsert_observation(obs: DailyMaxObservation, db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations(station, date, source, value, units, fetched_ts, is_settlement_source) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(station, date, source) DO UPDATE SET "
            "  value=excluded.value, units=excluded.units, fetched_ts=excluded.fetched_ts,"
            "  is_settlement_source=excluded.is_settlement_source",
            (
                obs.station,
                obs.date,
                obs.source,
                obs.value,
                obs.units,
                int(time.time()),
                int(obs.is_settlement_source),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def native_to_c(value: float, units: str) -> float:
    """Convenience helper: defers to wxm.units to honor the sole-owner rule."""
    if units == "celsius":
        return value
    if units == "fahrenheit":
        return f_to_c(value)
    raise ValueError(f"unknown units: {units!r}")


# ----------------------------------------------------------------- Orchestration


def fetch_truth(
    spec: Spec,
    target_date: str,
    db_path: Path = DEFAULT_DB_PATH,
    client: httpx.Client | None = None,
    wu_api_key: str | None = None,
) -> dict[str, list[str]]:
    """For each station, fetch settlement-grade observation(s) for target_date.

    Returns {station_id: [source_names_persisted]}.
    """
    out: dict[str, list[str]] = {sid: [] for sid in spec.resolution.cities}
    owned = client is None
    if owned:
        client = httpx.Client()
    try:
        for station_id, city in spec.resolution.cities.items():
            kind = city.settlement.source_kind

            if kind == "hko_daily_extract":
                xml_text = fetch_hko(target_date, client)
                if xml_text is None:
                    continue
                val_c = parse_hko_monthly_xml(xml_text, target_date)
                if val_c is None:
                    continue
                upsert_observation(
                    DailyMaxObservation(station_id, target_date, "hko_extract", val_c, "celsius", True),
                    db_path,
                )
                out[station_id].append("hko_extract")

            elif kind == "wunderground_daily":
                if wu_api_key is None:
                    # Caller is responsible for providing the apiKey; skip if not given
                    continue
                units_hint = "m" if city.settlement.units == "celsius" else "e"
                payload = fetch_wu(
                    city.settlement.station_id, target_date, units_hint, wu_api_key, client
                )
                if payload is None:
                    continue
                parsed = parse_wu_historical_json(payload, units_hint)
                if parsed is None:
                    continue
                value, units = parsed
                upsert_observation(
                    DailyMaxObservation(
                        station_id, target_date, "wunderground", value, units, True,
                    ),
                    db_path,
                )
                out[station_id].append("wunderground")

                # Cross-check for KNYC: NWS CLI from NYC office (OKX)
                if city.settlement.station_id == "KNYC":
                    cli_text = fetch_nws_cli("OKX", target_date, client)
                    if cli_text is not None:
                        val_f = parse_nws_cli_text(cli_text, target_date)
                        if val_f is not None:
                            upsert_observation(
                                DailyMaxObservation(
                                    station_id, target_date, "nws_cli", val_f, "fahrenheit", False,
                                ),
                                db_path,
                            )
                            out[station_id].append("nws_cli")
    finally:
        if owned:
            client.close()
    return out
