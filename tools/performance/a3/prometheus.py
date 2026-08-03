"""A3 Prometheus range-query layer (standard library only).

Queries ``<base_url>/api/v1/query_range`` via urllib with an explicit
timeout and a bounded 16 MiB response read. Responses are validated
strictly: matrix result type, finite numeric samples, unique timestamps per
series, and unique canonical series identity. Series are returned in
deterministic canonical-identity order.
"""

import dataclasses
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

A3_STEP_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMESTAMP_TOLERANCE = 1e-6

_SAFE_SCHEMES = ("http", "https")


class PrometheusError(Exception):
    """Base class for Prometheus client errors."""


class PrometheusHTTPError(PrometheusError):
    """Transport-level failure (HTTP, URL, timeout, oversized body)."""


class PrometheusResponseError(PrometheusError):
    """Response validation failure."""


class DuplicateSeriesError(PrometheusResponseError):
    """Two returned series share the same canonical identity."""


@dataclasses.dataclass(frozen=True)
class MetricSample:
    timestamp: float
    value: float


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def canonical_identity(labels: Mapping[str, str]) -> str:
    """Canonical series identity with sorted labels and safe escaping."""
    parts = ",".join(
        f'{_escape_label(key)}="{_escape_label(labels[key])}"'
        for key in sorted(labels)
        if key != "__name__"
    )
    name = labels.get("__name__")
    if name:
        return f"{_escape_label(name)}{{{parts}}}"
    return f"{{{parts}}}"


@dataclasses.dataclass(frozen=True)
class MetricSeries:
    labels: Mapping[str, str]
    samples: Tuple[MetricSample, ...]
    identity: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        object.__setattr__(self, "samples", tuple(self.samples))
        object.__setattr__(self, "identity", canonical_identity(self.labels))


@dataclasses.dataclass(frozen=True)
class CounterReset:
    identity: str
    previous_timestamp: float
    previous_value: float
    timestamp: float
    value: float


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects to non-HTTP(S) schemes (e.g. file://)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme
        if scheme not in _SAFE_SCHEMES:
            raise PrometheusHTTPError(
                f"redirect to unsupported scheme: {scheme or 'unknown'}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> Callable:
    return urllib.request.build_opener(_SafeRedirectHandler()).open


class PrometheusClient:
    """Strict, injectable-transport Prometheus query_range client."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        opener: Optional[Callable] = None,
    ) -> None:
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise PrometheusError(
                f"timeout_seconds must be a positive integer, got {timeout_seconds!r}"
            )
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in _SAFE_SCHEMES:
            raise PrometheusError(
                f"base_url scheme must be http or https, got {parsed.scheme!r}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise PrometheusError("base_url must not contain credentials")
        if parsed.fragment:
            raise PrometheusError("base_url must not contain a fragment")
        if not parsed.netloc:
            raise PrometheusError("base_url must contain a host")
        base = base_url[:-1] if base_url.endswith("/") else base_url
        self._base_url = base
        self._timeout_seconds = timeout_seconds
        self._opener = opener or _default_opener()

    def query_range(
        self,
        expr: str,
        start: Any,
        end: Any,
        step: int = A3_STEP_SECONDS,
    ) -> Tuple[MetricSeries, ...]:
        if not isinstance(expr, str) or not expr:
            raise PrometheusError("expr must be a non-empty string")
        for bound, name in ((start, "start"), (end, "end")):
            if (
                not isinstance(bound, (int, float))
                or isinstance(bound, bool)
                or not math.isfinite(bound)
            ):
                raise PrometheusError(f"{name} must be a finite number, got {bound!r}")
        if end < start:
            raise PrometheusError("end must be >= start")
        if step != A3_STEP_SECONDS:
            raise PrometheusError(
                f"step must be exactly {A3_STEP_SECONDS} for A3, got {step!r}"
            )

        params = urllib.parse.urlencode(
            {"query": expr, "start": start, "end": end, "step": step}
        )
        url = f"{self._base_url}/api/v1/query_range?{params}"
        request = urllib.request.Request(url, method="GET")
        body = self._fetch(request)
        return _parse_matrix(body)

    def _fetch(self, request) -> bytes:
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
        except urllib.error.HTTPError as exc:
            raise PrometheusHTTPError(f"HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PrometheusHTTPError(f"URL error: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            # socket.timeout is an OSError subclass on modern Python.
            raise PrometheusHTTPError(f"request failed: {exc}") from exc
        status = getattr(response, "status", None)
        if status is not None and not 200 <= status < 300:
            raise PrometheusHTTPError(f"unexpected HTTP status {status}")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise PrometheusHTTPError(
                f"response exceeds {MAX_RESPONSE_BYTES} bytes"
            )
        return body


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise PrometheusResponseError(f"{field} must be numeric")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            raise PrometheusResponseError(
                f"{field} is not parseable as a float"
            ) from None
    else:
        raise PrometheusResponseError(f"{field} must be numeric")
    if not math.isfinite(result):
        raise PrometheusResponseError(f"{field} must be finite")
    return result


def _parse_matrix(body: bytes) -> Tuple[MetricSeries, ...]:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PrometheusResponseError("response is not valid UTF-8") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise PrometheusResponseError("response is not valid JSON") from None

    if not isinstance(payload, dict):
        raise PrometheusResponseError("response root must be an object")
    if payload.get("status") != "success":
        raise PrometheusResponseError("status is not success")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PrometheusResponseError("data must be an object")
    if data.get("resultType") != "matrix":
        raise PrometheusResponseError("resultType must be matrix")
    result = data.get("result")
    if not isinstance(result, list):
        raise PrometheusResponseError("data.result must be a list")

    series_list: List[MetricSeries] = []
    for entry in result:
        if not isinstance(entry, dict):
            raise PrometheusResponseError("series entry must be an object")
        metric = entry.get("metric")
        if not isinstance(metric, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metric.items()
        ):
            raise PrometheusResponseError("malformed labels")
        values = entry.get("values")
        if not isinstance(values, list):
            raise PrometheusResponseError("series values must be a list")
        samples: List[MetricSample] = []
        seen_timestamps = set()
        for sample in values:
            if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                raise PrometheusResponseError("malformed sample")
            timestamp = _parse_number(sample[0], "sample timestamp")
            value = _parse_number(sample[1], "sample value")
            if timestamp in seen_timestamps:
                raise PrometheusResponseError("duplicate timestamp in series")
            seen_timestamps.add(timestamp)
            samples.append(MetricSample(timestamp, value))
        samples.sort(key=lambda sample: sample.timestamp)
        series_list.append(
            MetricSeries(labels=metric, samples=tuple(samples))
        )

    series_list.sort(key=lambda series: series.identity)
    identities = [series.identity for series in series_list]
    for previous, current in zip(identities, identities[1:]):
        if previous == current:
            raise DuplicateSeriesError(
                f"duplicate series identity: {current}"
            )
    return tuple(series_list)


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def detect_missing_samples(
    series: MetricSeries,
    start: float,
    end: float,
    step: int = A3_STEP_SECONDS,
) -> Tuple[float, ...]:
    """Expected timestamps in [start, end] with no matching sample.

    Samples outside the requested range do not satisfy expected points;
    matching allows a maximum timestamp tolerance of 1e-6.
    """
    expected: List[float] = []
    current = start
    while current <= end:
        expected.append(current)
        current = current + step

    present = sorted(
        sample.timestamp
        for sample in series.samples
        if start - TIMESTAMP_TOLERANCE <= sample.timestamp <= end + TIMESTAMP_TOLERANCE
    )
    missing: List[float] = []
    for point in expected:
        index = bisect_left(present, point - TIMESTAMP_TOLERANCE)
        if index >= len(present) or abs(present[index] - point) > TIMESTAMP_TOLERANCE:
            missing.append(point)
    return tuple(missing)


def detect_counter_resets(series: MetricSeries) -> Tuple[CounterReset, ...]:
    """A reset occurs when a counter value decreases vs the previous sample."""
    resets: List[CounterReset] = []
    samples = sorted(series.samples, key=lambda sample: sample.timestamp)
    for previous, current in zip(samples, samples[1:]):
        if current.value < previous.value:
            resets.append(
                CounterReset(
                    identity=series.identity,
                    previous_timestamp=previous.timestamp,
                    previous_value=previous.value,
                    timestamp=current.timestamp,
                    value=current.value,
                )
            )
    return tuple(resets)
