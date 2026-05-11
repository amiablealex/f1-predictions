"""Client for the Jolpica F1 API (Ergast successor).

Endpoint surface used by the predictions app:
  GET /{season}.json                                  → schedule with sessions
  GET /{season}/drivers.json                          → driver list
  GET /{season}/{round}/qualifying.json               → quali results
  GET /{season}/{round}/results.json                  → race results
  GET /{season}/{round}/sprint.json                   → sprint race results
  GET /{season}/{round}/sprint/qualifying.json        → sprint quali results
  GET /{season}/{round}/pitstops.json                 → pit-stop records
                                                        (verify endpoint shape
                                                         the first time you
                                                         deploy — Jolpica
                                                         occasionally tweaks
                                                         sprint endpoints)

Implements:
  - process-wide rate limiting (default 0.3s between requests)
  - retry on 429 / network errors with exponential backoff
  - typed dataclasses returned to callers (no raw dicts leak out)
  - lap time strings ("1:23.456") parsed to integer milliseconds

The client is instantiated by the worker and by tests. Tests mock at the
`requests.get` level rather than constructing fake JolpicaClients.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from app.api.exceptions import (
    JolpicaNotFoundError,
    JolpicaParseError,
    JolpicaRateLimitError,
    JolpicaTransientError,
)

log = logging.getLogger(__name__)


# =============================================================================
# Country-name → ISO-2 mapping for flag emoji
# Covers every country to host an F1 round in the modern era plus a few
# historical entries. Any unknown name returns None and the UI falls back to
# no flag — admin can correct via the round edit screen if needed.
# =============================================================================

_COUNTRY_TO_ISO2 = {
    "australia": "AU",
    "austria": "AT",
    "azerbaijan": "AZ",
    "bahrain": "BH",
    "belgium": "BE",
    "brazil": "BR",
    "canada": "CA",
    "china": "CN",
    "france": "FR",
    "germany": "DE",
    "hungary": "HU",
    "italy": "IT",
    "japan": "JP",
    "korea": "KR",
    "south korea": "KR",
    "malaysia": "MY",
    "mexico": "MX",
    "monaco": "MC",
    "morocco": "MA",
    "netherlands": "NL",
    "portugal": "PT",
    "qatar": "QA",
    "russia": "RU",
    "saudi arabia": "SA",
    "singapore": "SG",
    "south africa": "ZA",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "turkey": "TR",
    "uae": "AE",
    "united arab emirates": "AE",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "usa": "US",
    "vietnam": "VN",
}


def country_to_iso2(country_name: str | None) -> str | None:
    if not country_name:
        return None
    return _COUNTRY_TO_ISO2.get(country_name.strip().lower())


# =============================================================================
# Typed result dataclasses (returned to callers — no raw dicts leak out)
# =============================================================================


@dataclass(frozen=True)
class APIDriver:
    driver_ref: str            # Jolpica's stable driverId, e.g. "hamilton"
    given_name: str
    family_name: str
    code: str | None           # e.g. "HAM"
    permanent_number: int | None
    nationality: str | None


@dataclass(frozen=True)
class APIRoundEntry:
    """One car entry in a round's session — driver, car number, constructor."""
    driver: APIDriver
    car_number: int
    constructor_name: str | None


@dataclass(frozen=True)
class APIScheduledSession:
    session_type: str          # "sprint_quali" | "sprint_race" | "qualifying" | "race"
    scheduled_start: datetime  # tz-aware UTC


@dataclass(frozen=True)
class APIRound:
    season: int
    round_number: int
    gp_name: str
    country: str | None
    country_code: str | None
    circuit_name: str | None
    circuit_ref: str | None
    has_sprint: bool
    sessions: list[APIScheduledSession]


@dataclass(frozen=True)
class APIQualifyingEntry:
    position: int
    car_number: int
    driver_ref: str
    best_time_ms: int | None   # min of Q1/Q2/Q3 actually set


@dataclass(frozen=True)
class APIRaceEntry:
    position: int
    car_number: int
    driver_ref: str
    constructor_name: str | None
    status: str
    is_classified: bool        # True for "Finished" / "+N Lap(s)"
    is_fastest_lap: bool        # only one entry per race
    grid: int | None = None    # starting grid position
    laps_completed: int | None = None  # laps the driver completed
    race_time_ms: int | None = None    # Jolpica's Time.millis (total race time)

@dataclass(frozen=True)
class APIPitStop:
    """One pit-stop entry from Jolpica's /pitstops endpoint.

    Note: Jolpica's `duration` includes pit lane transit time, not just
    the stationary-stop time. Useful for "most pit stops" / "lap of first
    pit stop" / etc., but not a measure of crew speed.
    """
    driver_ref: str
    lap: int
    stop_number: int          # 1-indexed (matches Jolpica's "stop" field)
    duration_ms: int | None   # may be missing for some historical races

# =============================================================================
# Lap time parsing
# =============================================================================

_LAP_TIME_PATTERN = re.compile(r"^(?:(\d+):)?(\d+)\.(\d+)$")


def parse_lap_time(value: str | None) -> int | None:
    """Parse a lap time ('1:23.456' or '23.456') into integer milliseconds.

    Returns None for empty / placeholder / unparseable values.
    """
    if value is None:
        return None
    s = value.strip()
    if s == "" or s == "-":
        return None
    m = _LAP_TIME_PATTERN.match(s)
    if not m:
        return None
    minutes_part, seconds_part, fractional_part = m.groups()
    minutes = int(minutes_part) if minutes_part else 0
    seconds = int(seconds_part)
    fractional = (fractional_part + "000")[:3]   # pad/truncate to 3 digits
    return minutes * 60_000 + seconds * 1000 + int(fractional)


def format_lap_time(ms: int | None) -> str:
    """Inverse of parse_lap_time: format ms into 'M:SS.mmm' or 'SS.mmm'."""
    if ms is None:
        return "—"
    minutes, remainder = divmod(ms, 60_000)
    seconds, millis = divmod(remainder, 1000)
    if minutes:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


# =============================================================================
# Race result classification
# =============================================================================

# A finishing status is "classified" if it's "Finished" or "+N Lap(s)".
# Anything else (mechanical retirement, accident, DNS, DSQ) is treated as a
# DNF for DNF-count scoring and as unclassified for position scoring.
_CLASSIFIED_STATUS_PATTERN = re.compile(r"^(Finished|Lapped|\+\d+\s+Laps?)$", re.IGNORECASE)


def is_status_classified(status: str) -> bool:
    return bool(_CLASSIFIED_STATUS_PATTERN.match(status.strip()))


# =============================================================================
# HTTP client
# =============================================================================


class JolpicaClient:
    """Thin REST client with global rate limiting and basic retry."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        min_request_interval_seconds: float = 0.3,
        timeout_seconds: int = 15,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.min_interval = min_request_interval_seconds
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    # -------------------------------------------------------------- transport

    def _wait_for_rate_limit(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_request_at = time.monotonic()

    def _get_json(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait_for_rate_limit()
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                log.warning("Jolpica network error on %s: %s", url, exc)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                sleep_for = 2 ** (attempt + 1)
                log.warning("Jolpica 429 on %s; sleeping %ds", url, sleep_for)
                time.sleep(sleep_for)
                last_exc = JolpicaRateLimitError(f"429 from {url}")
                continue

            if 500 <= resp.status_code < 600:
                last_exc = JolpicaTransientError(f"{resp.status_code} from {url}")
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 404:
                raise JolpicaNotFoundError(f"404 from {url}")

            try:
                resp.raise_for_status()
                return resp.json()
            except (ValueError, requests.exceptions.HTTPError) as exc:
                raise JolpicaParseError(f"Bad response from {url}: {exc}") from exc

        raise JolpicaTransientError(
            f"Jolpica request failed after {self.max_retries} attempts: {url}"
        ) from last_exc

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _race_table(payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return payload["MRData"]["RaceTable"]["Races"]
        except (KeyError, TypeError) as exc:
            raise JolpicaParseError("Missing MRData.RaceTable.Races") from exc

    @staticmethod
    def _parse_session_dt(date_str: str | None, time_str: str | None) -> datetime | None:
        """Combine an Ergast-style date+time pair into a tz-aware UTC datetime."""
        if not date_str:
            return None
        time_part = time_str or "00:00:00Z"
        if time_part.endswith("Z"):
            time_part = time_part[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(f"{date_str}T{time_part}")
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_driver(d: dict[str, Any]) -> APIDriver:
        try:
            permanent = d.get("permanentNumber")
            return APIDriver(
                driver_ref=d["driverId"],
                given_name=d["givenName"],
                family_name=d["familyName"],
                code=d.get("code"),
                permanent_number=int(permanent) if permanent else None,
                nationality=d.get("nationality"),
            )
        except KeyError as exc:
            raise JolpicaParseError(f"Driver missing field: {exc}") from exc

    # =========================================================================
    # Public methods
    # =========================================================================

    def get_season_schedule(self, season: int) -> list[APIRound]:
        """Return the full season schedule with session start times."""
        payload = self._get_json(f"/{season}.json")
        races = self._race_table(payload)
        rounds: list[APIRound] = []
        for race in races:
            try:
                round_number = int(race["round"])
                gp_name = race["raceName"]
            except (KeyError, ValueError) as exc:
                raise JolpicaParseError(f"Race entry missing fields: {exc}") from exc

            circuit = race.get("Circuit", {}) or {}
            location = circuit.get("Location", {}) or {}
            country_name = location.get("country")

            sessions: list[APIScheduledSession] = []

            quali_dt = self._parse_session_dt(
                race.get("Qualifying", {}).get("date"),
                race.get("Qualifying", {}).get("time"),
            )
            if quali_dt:
                sessions.append(APIScheduledSession("qualifying", quali_dt))

            race_dt = self._parse_session_dt(race.get("date"), race.get("time"))
            if race_dt:
                sessions.append(APIScheduledSession("race", race_dt))

            sprint_block = race.get("Sprint")
            sprint_quali_block = (
                race.get("SprintQualifying")
                or race.get("SprintShootout")  # 2023 legacy name
            )
            has_sprint = bool(sprint_block)

            if sprint_quali_block:
                sq_dt = self._parse_session_dt(
                    sprint_quali_block.get("date"), sprint_quali_block.get("time"),
                )
                if sq_dt:
                    sessions.append(APIScheduledSession("sprint_quali", sq_dt))
            if sprint_block:
                sr_dt = self._parse_session_dt(sprint_block.get("date"), sprint_block.get("time"))
                if sr_dt:
                    sessions.append(APIScheduledSession("sprint_race", sr_dt))

            sessions.sort(key=lambda s: s.scheduled_start)

            rounds.append(APIRound(
                season=season,
                round_number=round_number,
                gp_name=gp_name,
                country=country_name,
                country_code=country_to_iso2(country_name),
                circuit_name=circuit.get("circuitName"),
                circuit_ref=circuit.get("circuitId"),
                has_sprint=has_sprint,
                sessions=sessions,
            ))
        rounds.sort(key=lambda r: r.round_number)
        return rounds

    def get_season_drivers(self, season: int) -> list[APIDriver]:
        """Return the master driver list for a season."""
        payload = self._get_json(f"/{season}/drivers.json")
        try:
            drivers_list = payload["MRData"]["DriverTable"]["Drivers"]
        except (KeyError, TypeError) as exc:
            raise JolpicaParseError("Missing MRData.DriverTable.Drivers") from exc
        return [self._parse_driver(d) for d in drivers_list]

    def get_qualifying_results(
        self, season: int, round_number: int
    ) -> list[APIQualifyingEntry]:
        return self._fetch_qualifying(f"/{season}/{round_number}/qualifying.json")

    # Note: Jolpica does not currently expose a sprint-qualifying endpoint
    # (as of mid-2026 they've said it's "coming"). The app does not score
    # sprint qualifying — sprint quali is treated as deadline-only.

    def _fetch_qualifying(self, path: str) -> list[APIQualifyingEntry]:
        payload = self._get_json(path)
        races = self._race_table(payload)
        if not races:
            raise JolpicaNotFoundError(f"No race in qualifying response: {path}")
        try:
            results = races[0]["QualifyingResults"]
        except (KeyError, TypeError) as exc:
            raise JolpicaParseError("Missing QualifyingResults") from exc

        entries: list[APIQualifyingEntry] = []
        for r in results:
            try:
                position = int(r["position"])
                car_number = int(r["number"])
                driver_ref = r["Driver"]["driverId"]
            except (KeyError, ValueError, TypeError) as exc:
                raise JolpicaParseError(f"Bad qualifying entry: {exc}") from exc
            q1 = parse_lap_time(r.get("Q1"))
            q2 = parse_lap_time(r.get("Q2"))
            q3 = parse_lap_time(r.get("Q3"))
            best = min((t for t in (q1, q2, q3) if t is not None), default=None)
            entries.append(APIQualifyingEntry(
                position=position,
                car_number=car_number,
                driver_ref=driver_ref,
                best_time_ms=best,
            ))
        entries.sort(key=lambda e: e.position)
        return entries

    def get_race_results(self, season: int, round_number: int) -> list[APIRaceEntry]:
        return self._fetch_race(f"/{season}/{round_number}/results.json", "Results")

    def get_sprint_race_results(self, season: int, round_number: int) -> list[APIRaceEntry]:
        return self._fetch_race(f"/{season}/{round_number}/sprint.json", "SprintResults")

    def get_pit_stops(self, season: int, round_number: int) -> list[APIPitStop]:
        """Return all pit stops for a round.

        Jolpica paginates pit-stop responses (default limit 30, max 100).
        A wet race can produce 60+ stops, so we request limit=100 and
        keep paging until we've seen the full `total`.
        """
        out: list[APIPitStop] = []
        limit = 100
        offset = 0
        while True:
            path = f"/{season}/{round_number}/pitstops.json?limit={limit}&offset={offset}"
            payload = self._get_json(path)
            races = self._race_table(payload)
            if not races:
                break
            stops = races[0].get("PitStops") or []
            for s in stops:
                try:
                    out.append(APIPitStop(
                        driver_ref=s["driverId"],
                        lap=int(s["lap"]),
                        stop_number=int(s["stop"]),
                        duration_ms=parse_lap_time(s.get("duration")),
                    ))
                except (KeyError, ValueError, TypeError) as exc:
                    raise JolpicaParseError(f"Bad pit stop entry: {exc}") from exc
            try:
                total = int(payload["MRData"].get("total", len(out)))
            except (ValueError, TypeError):
                total = len(out)
            offset += limit
            if offset >= total or not stops:
                break
        return out

    def _fetch_race(self, path: str, results_key: str) -> list[APIRaceEntry]:
        payload = self._get_json(path)
        races = self._race_table(payload)
        if not races:
            raise JolpicaNotFoundError(f"No race in response: {path}")
        try:
            results = races[0][results_key]
        except (KeyError, TypeError) as exc:
            raise JolpicaParseError(f"Missing {results_key}") from exc

        # Identify the fastest-lap setter (rank == "1" in FastestLap.rank).
        fastest_car_number: int | None = None
        for r in results:
            fl = r.get("FastestLap") or {}
            if fl.get("rank") == "1":
                try:
                    fastest_car_number = int(r["number"])
                except (KeyError, ValueError, TypeError):
                    pass
                break

        entries: list[APIRaceEntry] = []
        for r in results:
            try:
                position = int(r["position"])
                car_number = int(r["number"])
                driver_ref = r["Driver"]["driverId"]
                status = r.get("status", "Unknown")
            except (KeyError, ValueError, TypeError) as exc:
                raise JolpicaParseError(f"Bad race entry: {exc}") from exc
            constructor = (r.get("Constructor") or {}).get("name")
            try:
                grid_raw = r.get("grid")
                grid = int(grid_raw) if grid_raw is not None else None
            except (ValueError, TypeError):
                grid = None
            try:
                laps_raw = r.get("laps")
                laps_completed = int(laps_raw) if laps_raw is not None else None
            except (ValueError, TypeError):
                laps_completed = None
            try:
                time_block = r.get("Time") or {}
                millis_raw = time_block.get("millis")
                race_time_ms = int(millis_raw) if millis_raw is not None else None
            except (ValueError, TypeError):
                race_time_ms = None
            entries.append(APIRaceEntry(
                position=position,
                car_number=car_number,
                driver_ref=driver_ref,
                constructor_name=constructor,
                status=status,
                is_classified=is_status_classified(status),
                is_fastest_lap=(fastest_car_number is not None and car_number == fastest_car_number),
                grid=grid,
                laps_completed=laps_completed,
                race_time_ms=race_time_ms,
            ))
        entries.sort(key=lambda e: e.position)
        return entries

    # ----- convenience: build round entry list from race or quali results ----

    def get_round_entries(
        self, season: int, round_number: int, prefer_session: str = "race"
    ) -> list[APIRoundEntry]:
        """Return car-number / driver / constructor entries for a completed round.

        Used by the worker to seed RoundDriver after the round's first
        scoring session has produced results. `prefer_session` chooses which
        session to pull entries from; falls through to qualifying if race
        isn't available yet.
        """
        attempts: list[tuple[str, str]] = []
        if prefer_session == "race":
            attempts = [("race", f"/{season}/{round_number}/results.json"),
                        ("qualifying", f"/{season}/{round_number}/qualifying.json")]
        else:
            attempts = [("qualifying", f"/{season}/{round_number}/qualifying.json"),
                        ("race", f"/{season}/{round_number}/results.json")]

        last_err: Exception | None = None
        for label, path in attempts:
            try:
                payload = self._get_json(path)
            except JolpicaNotFoundError as exc:
                last_err = exc
                continue
            races = self._race_table(payload)
            if not races:
                continue
            results_key = "Results" if label == "race" else "QualifyingResults"
            results = races[0].get(results_key) or []
            if not results:
                continue
            entries: list[APIRoundEntry] = []
            for r in results:
                try:
                    car_number = int(r["number"])
                    driver = self._parse_driver(r["Driver"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise JolpicaParseError(f"Bad entry row: {exc}") from exc
                constructor = (r.get("Constructor") or {}).get("name")
                entries.append(APIRoundEntry(
                    driver=driver,
                    car_number=car_number,
                    constructor_name=constructor,
                ))
            return entries
        raise JolpicaNotFoundError(
            f"No entry data for {season}/{round_number}"
        ) from last_err


def build_default_client(config) -> JolpicaClient:
    """Construct a client from a Flask config object."""
    return JolpicaClient(
        base_url=config["JOLPICA_BASE_URL"],
        user_agent=config["JOLPICA_USER_AGENT"],
        min_request_interval_seconds=config["JOLPICA_MIN_REQUEST_INTERVAL_SECONDS"],
        timeout_seconds=config["JOLPICA_REQUEST_TIMEOUT_SECONDS"],
    )
