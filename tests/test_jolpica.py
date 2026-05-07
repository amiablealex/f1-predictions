"""Tests for the Jolpica API client.

These mock at the `requests.get` level so we exercise the parsing and
classification logic without hitting the network.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.api.exceptions import (
    JolpicaNotFoundError,
    JolpicaParseError,
    JolpicaRateLimitError,
    JolpicaTransientError,
)
from app.api.jolpica import (
    JolpicaClient,
    country_to_iso2,
    format_lap_time,
    is_status_classified,
    parse_lap_time,
)


# =============================================================================
# parse_lap_time
# =============================================================================


@pytest.mark.parametrize(
    "raw,expected_ms",
    [
        ("1:23.456", 83_456),
        ("0:23.456", 23_456),
        ("23.456", 23_456),
        ("1:00.000", 60_000),
        ("0:00.001", 1),
        ("1:23.4", 83_400),     # millisecond precision auto-padded
        ("", None),
        ("-", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_lap_time(raw, expected_ms):
    assert parse_lap_time(raw) == expected_ms


def test_format_lap_time_round_trips():
    assert format_lap_time(83_456) == "1:23.456"
    assert format_lap_time(23_456) == "23.456"
    assert format_lap_time(None) == "—"


def test_lap_time_round_trip():
    """parse → format → parse yields the original."""
    original = "1:23.456"
    ms = parse_lap_time(original)
    formatted = format_lap_time(ms)
    assert parse_lap_time(formatted) == ms


# =============================================================================
# country_to_iso2
# =============================================================================


def test_country_mapping():
    assert country_to_iso2("Australia") == "AU"
    assert country_to_iso2("united kingdom") == "GB"
    assert country_to_iso2("UK") == "GB"
    assert country_to_iso2("United States") == "US"
    assert country_to_iso2("USA") == "US"
    assert country_to_iso2("Atlantis") is None
    assert country_to_iso2(None) is None
    assert country_to_iso2("") is None


# =============================================================================
# is_status_classified
# =============================================================================


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Finished", True),
        ("Lapped", True),
        ("+1 Lap", True),
        ("+2 Laps", True),
        ("+10 Laps", True),
        ("Engine", False),
        ("Accident", False),
        ("Did not start", False),
        ("Disqualified", False),
        ("Retired", False),
        ("Collision", False),
    ],
)
def test_classification(status, expected):
    assert is_status_classified(status) == expected


# =============================================================================
# Schedule parsing
# =============================================================================


def _mock_response(json_payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_payload
    resp.raise_for_status = MagicMock()
    return resp


def _client():
    # Tiny request interval so tests don't sleep
    return JolpicaClient(
        base_url="https://example.test/ergast/f1",
        user_agent="test-agent",
        min_request_interval_seconds=0.0,
        timeout_seconds=5,
    )


def test_get_season_schedule_parses_standard_and_sprint_weekends():
    payload = {
        "MRData": {
            "RaceTable": {
                "season": "2026",
                "Races": [
                    {
                        "season": "2026",
                        "round": "1",
                        "raceName": "Australian Grand Prix",
                        "Circuit": {
                            "circuitId": "albert_park",
                            "circuitName": "Albert Park Grand Prix Circuit",
                            "Location": {"country": "Australia"},
                        },
                        "date": "2026-03-08",
                        "time": "04:00:00Z",
                        "Qualifying": {"date": "2026-03-07", "time": "05:00:00Z"},
                    },
                    {
                        "season": "2026",
                        "round": "5",
                        "raceName": "Miami Grand Prix",
                        "Circuit": {
                            "circuitId": "miami",
                            "circuitName": "Miami International Autodrome",
                            "Location": {"country": "United States"},
                        },
                        "date": "2026-05-04",
                        "time": "19:30:00Z",
                        "Qualifying": {"date": "2026-05-03", "time": "20:00:00Z"},
                        "Sprint": {"date": "2026-05-03", "time": "16:00:00Z"},
                        "SprintQualifying": {"date": "2026-05-02", "time": "20:30:00Z"},
                    },
                ],
            }
        }
    }
    with patch("app.api.jolpica.requests.get", return_value=_mock_response(payload)):
        rounds = _client().get_season_schedule(2026)

    assert len(rounds) == 2

    standard = rounds[0]
    assert standard.round_number == 1
    assert standard.gp_name == "Australian Grand Prix"
    assert standard.country == "Australia"
    assert standard.country_code == "AU"
    assert standard.has_sprint is False
    session_types = [s.session_type for s in standard.sessions]
    assert "qualifying" in session_types
    assert "race" in session_types
    assert "sprint_quali" not in session_types

    sprint = rounds[1]
    assert sprint.round_number == 5
    assert sprint.has_sprint is True
    sprint_types = [s.session_type for s in sprint.sessions]
    assert "sprint_quali" in sprint_types
    assert "sprint_race" in sprint_types
    assert "qualifying" in sprint_types
    assert "race" in sprint_types
    # Sessions should be in chronological order
    starts = [s.scheduled_start for s in sprint.sessions]
    assert starts == sorted(starts)


def test_get_season_schedule_handles_legacy_sprint_shootout_field():
    """2023 used 'SprintShootout'; client falls back to it when present."""
    payload = {
        "MRData": {
            "RaceTable": {
                "Races": [{
                    "round": "1", "raceName": "Test GP",
                    "Circuit": {"circuitId": "x", "circuitName": "X", "Location": {"country": "Italy"}},
                    "date": "2023-04-30", "time": "13:00:00Z",
                    "Qualifying": {"date": "2023-04-29", "time": "14:00:00Z"},
                    "Sprint": {"date": "2023-04-29", "time": "15:30:00Z"},
                    "SprintShootout": {"date": "2023-04-29", "time": "11:00:00Z"},
                }]
            }
        }
    }
    with patch("app.api.jolpica.requests.get", return_value=_mock_response(payload)):
        rounds = _client().get_season_schedule(2023)
    sprint_types = [s.session_type for s in rounds[0].sessions]
    assert "sprint_quali" in sprint_types


# =============================================================================
# Qualifying parsing
# =============================================================================


def test_get_qualifying_results_picks_best_of_q1q2q3():
    payload = {
        "MRData": {
            "RaceTable": {
                "Races": [{
                    "QualifyingResults": [
                        {
                            "position": "1", "number": "44",
                            "Driver": {"driverId": "hamilton"},
                            "Q1": "1:24.000", "Q2": "1:23.500", "Q3": "1:23.456",
                        },
                        {
                            "position": "2", "number": "1",
                            "Driver": {"driverId": "max_verstappen"},
                            "Q1": "1:23.900", "Q2": "1:23.700", "Q3": "1:23.500",
                        },
                        {
                            "position": "16", "number": "23",
                            "Driver": {"driverId": "albon"},
                            "Q1": "1:25.300",
                            # No Q2 / Q3 — eliminated
                        },
                    ]
                }]
            }
        }
    }
    with patch("app.api.jolpica.requests.get", return_value=_mock_response(payload)):
        results = _client().get_qualifying_results(2026, 1)
    assert results[0].best_time_ms == 83_456    # Q3
    assert results[1].best_time_ms == 83_500
    assert results[2].best_time_ms == 85_300    # only Q1 set


# =============================================================================
# Race results parsing
# =============================================================================


def test_get_race_results_identifies_fastest_lap_setter():
    payload = {
        "MRData": {
            "RaceTable": {
                "Races": [{
                    "Results": [
                        {"position": "1", "number": "44", "Driver": {"driverId": "hamilton"},
                         "Constructor": {"name": "Mercedes"}, "status": "Finished",
                         "FastestLap": {"rank": "2"}},
                        {"position": "2", "number": "1", "Driver": {"driverId": "max_verstappen"},
                         "Constructor": {"name": "Red Bull"}, "status": "Finished",
                         "FastestLap": {"rank": "1"}},
                        {"position": "20", "number": "23", "Driver": {"driverId": "albon"},
                         "Constructor": {"name": "Williams"}, "status": "Engine"},
                    ]
                }]
            }
        }
    }
    with patch("app.api.jolpica.requests.get", return_value=_mock_response(payload)):
        results = _client().get_race_results(2026, 1)

    assert results[0].is_fastest_lap is False
    assert results[1].is_fastest_lap is True
    assert results[2].is_fastest_lap is False
    # Classification flags
    assert results[0].is_classified is True
    assert results[1].is_classified is True
    assert results[2].is_classified is False


# =============================================================================
# Error handling
# =============================================================================


def test_404_raises_not_found():
    with patch(
        "app.api.jolpica.requests.get",
        return_value=_mock_response({}, status_code=404),
    ):
        with pytest.raises(JolpicaNotFoundError):
            _client().get_season_schedule(1900)


def test_429_with_eventual_success():
    """Client retries 429s up to max_retries before failing."""
    rate_limited = _mock_response({}, status_code=429)
    success = _mock_response({"MRData": {"RaceTable": {"Races": []}}})
    call_log = [rate_limited, success]

    def side_effect(*args, **kwargs):
        return call_log.pop(0)

    with patch("app.api.jolpica.requests.get", side_effect=side_effect):
        with patch("app.api.jolpica.time.sleep"):  # avoid waiting in tests
            rounds = _client().get_season_schedule(2026)
    assert rounds == []


def test_persistent_429_raises_transient_error():
    rate_limited = _mock_response({}, status_code=429)
    with patch("app.api.jolpica.requests.get", return_value=rate_limited):
        with patch("app.api.jolpica.time.sleep"):
            with pytest.raises(JolpicaTransientError):
                _client().get_season_schedule(2026)


def test_malformed_json_raises_parse_error():
    """Schema mismatch is treated as a hard failure."""
    bad_payload = {"unexpected": "structure"}
    with patch("app.api.jolpica.requests.get", return_value=_mock_response(bad_payload)):
        with pytest.raises(JolpicaParseError):
            _client().get_season_schedule(2026)
