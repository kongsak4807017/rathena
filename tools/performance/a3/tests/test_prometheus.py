"""Tests for the A3 Prometheus query layer and scrape configuration."""

import io
import json
import unittest
import urllib.error
from pathlib import Path

from tools.performance.a3.prometheus import (
    CounterReset,
    DuplicateSeriesError,
    MetricSample,
    MetricSeries,
    PrometheusClient,
    PrometheusError,
    PrometheusHTTPError,
    PrometheusResponseError,
    detect_counter_resets,
    detect_missing_samples,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "prometheus.yml"


def load_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int = 200):
        super().__init__(payload)
        self.status = status


class RecordingOpener:
    def __init__(self, payload: bytes, status: int = 200, error: Exception = None):
        self.payload = payload
        self.status = status
        self.error = error
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"request": request, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, status=self.status)


def make_client(opener, base_url="http://127.0.0.1:9090", timeout_seconds=10):
    return PrometheusClient(
        base_url=base_url, timeout_seconds=timeout_seconds, opener=opener
    )


class RequestConstructionTests(unittest.TestCase):
    def test_exact_url_parameters_and_timeout(self):
        opener = RecordingOpener(load_fixture("prometheus-range-success.json"))
        client = make_client(opener, base_url="http://127.0.0.1:9090/")
        client.query_range("up", 1000, 1010, step=5)
        call = opener.calls[0]
        self.assertEqual(
            call["request"].full_url,
            "http://127.0.0.1:9090/api/v1/query_range"
            "?query=up&start=1000&end=1010&step=5",
        )
        self.assertEqual(call["request"].get_method(), "GET")
        self.assertEqual(call["timeout"], 10)

    def test_expression_url_encoded(self):
        opener = RecordingOpener(load_fixture("prometheus-range-success.json"))
        client = make_client(opener)
        client.query_range('rate(http_requests_total{job="node"}[5m])', 1, 2)
        url = opener.calls[0]["request"].full_url
        self.assertIn("query=rate%28http_requests_total%7Bjob%3D%22node%22%7D%5B5m%5D%29", url)

    def test_step_must_be_exactly_five(self):
        opener = RecordingOpener(b"{}")
        client = make_client(opener)
        for step in (1, 4, 6, 10):
            with self.assertRaises(PrometheusError):
                client.query_range("up", 0, 100, step=step)
        self.assertEqual(opener.calls, [])

    def test_empty_expression_rejected(self):
        client = make_client(RecordingOpener(b"{}"))
        with self.assertRaises(PrometheusError):
            client.query_range("", 0, 100)

    def test_non_numeric_bounds_rejected(self):
        client = make_client(RecordingOpener(b"{}"))
        with self.assertRaises(PrometheusError):
            client.query_range("up", "0", 100)
        with self.assertRaises(PrometheusError):
            client.query_range("up", True, 100)

    def test_end_before_start_rejected(self):
        client = make_client(RecordingOpener(b"{}"))
        with self.assertRaises(PrometheusError):
            client.query_range("up", 100, 50)

    def test_invalid_timeout_rejected(self):
        with self.assertRaises(PrometheusError):
            PrometheusClient("http://127.0.0.1:9090", timeout_seconds=0)
        with self.assertRaises(PrometheusError):
            PrometheusClient("http://127.0.0.1:9090", timeout_seconds=True)


class BaseUrlValidationTests(unittest.TestCase):
    def test_credentials_rejected(self):
        with self.assertRaises(PrometheusError):
            PrometheusClient("http://user:pass@127.0.0.1:9090")

    def test_unsupported_scheme_rejected(self):
        for url in ("ftp://127.0.0.1:9090", "file:///etc/prom", "127.0.0.1:9090"):
            with self.assertRaises(PrometheusError):
                PrometheusClient(url)

    def test_fragment_rejected(self):
        with self.assertRaises(PrometheusError):
            PrometheusClient("http://127.0.0.1:9090/#frag")

    def test_https_accepted(self):
        client = PrometheusClient("https://127.0.0.1:9090")
        self.assertEqual(client._base_url, "https://127.0.0.1:9090")


class ParsingTests(unittest.TestCase):
    def test_successful_single_series(self):
        opener = RecordingOpener(load_fixture("prometheus-range-success.json"))
        series = make_client(opener).query_range("up", 1000, 1015)
        self.assertEqual(len(series), 1)
        first = series[0]
        self.assertEqual(
            first.labels["__name__"], "up"
        )
        self.assertEqual(
            first.samples,
            (
                MetricSample(1000.0, 1.0),
                MetricSample(1005.0, 1.0),
                MetricSample(1015.0, 1.0),
            ),
        )

    def test_multiple_series_sorted_deterministically(self):
        opener = RecordingOpener(load_fixture("prometheus-range-multiple.json"))
        series = make_client(opener).query_range("node_load", 1000, 1010)
        identities = [item.identity for item in series]
        self.assertEqual(identities, sorted(identities))
        self.assertEqual(len(series), 2)

    def test_samples_sorted_ascending(self):
        opener = RecordingOpener(load_fixture("prometheus-range-multiple.json"))
        series = make_client(opener).query_range("node_load", 1000, 1010)
        for item in series:
            timestamps = [sample.timestamp for sample in item.samples]
            self.assertEqual(timestamps, sorted(timestamps))

    def test_identity_escaping(self):
        series = MetricSeries(
            labels={"__name__": "m\\y", 'we"ird': "va\nlue"},
            samples=(),
        )
        self.assertEqual(series.identity, 'm\\\\y{we\\"ird="va\\nlue"}')

    def test_identity_without_name(self):
        series = MetricSeries(labels={"job": "node"}, samples=())
        self.assertEqual(series.identity, '{job="node"}')

    def test_labels_immutable(self):
        series = MetricSeries(labels={"a": "b"}, samples=())
        with self.assertRaises(TypeError):
            series.labels["a"] = "c"

    def test_duplicate_timestamp_rejected(self):
        payload = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "up"},
                            "values": [[1000, "1"], [1000, "2"]],
                        }
                    ],
                },
            }
        ).encode()
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_duplicate_series_rejected(self):
        payload = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {"metric": {"__name__": "up", "i": "a"}, "values": []},
                        {"metric": {"i": "a", "__name__": "up"}, "values": []},
                    ],
                },
            }
        ).encode()
        with self.assertRaises(DuplicateSeriesError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)


class ErrorHandlingTests(unittest.TestCase):
    def test_status_error(self):
        opener = RecordingOpener(load_fixture("prometheus-error.json"))
        with self.assertRaises(PrometheusResponseError):
            make_client(opener).query_range("up", 0, 10)

    def test_result_type_error(self):
        payload = json.dumps(
            {"status": "success", "data": {"resultType": "vector", "result": []}}
        ).encode()
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_invalid_json(self):
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(b"not json")).query_range("up", 0, 10)

    def test_invalid_utf8(self):
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(b"\xff\xfe{}")).query_range("up", 0, 10)

    def test_malformed_metric(self):
        payload = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": "oops", "values": []}],
                },
            }
        ).encode()
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_malformed_sample(self):
        payload = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {"metric": {"__name__": "up"}, "values": [[1000, "1", "x"]]}
                    ],
                },
            }
        ).encode()
        with self.assertRaises(PrometheusResponseError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_non_finite_values_rejected(self):
        for bad in ("NaN", "+Inf", "-Inf"):
            payload = json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {"__name__": "up"},
                                "values": [[1000, bad]],
                            }
                        ],
                    },
                }
            ).encode()
            with self.assertRaises(PrometheusResponseError, msg=bad):
                make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_http_error(self):
        error = urllib.error.HTTPError(
            "http://x", 500, "boom", hdrs=None, fp=None
        )
        with self.assertRaises(PrometheusHTTPError):
            make_client(RecordingOpener(b"", error=error)).query_range("up", 0, 10)

    def test_url_error(self):
        error = urllib.error.URLError("connection refused")
        with self.assertRaises(PrometheusHTTPError):
            make_client(RecordingOpener(b"", error=error)).query_range("up", 0, 10)

    def test_timeout(self):
        with self.assertRaises(PrometheusHTTPError):
            make_client(
                RecordingOpener(b"", error=TimeoutError("timed out"))
            ).query_range("up", 0, 10)

    def test_non_2xx_status_rejected(self):
        opener = RecordingOpener(b"{}", status=500)
        with self.assertRaises(PrometheusHTTPError):
            make_client(opener).query_range("up", 0, 10)

    def test_response_too_large(self):
        payload = b" " * (16 * 1024 * 1024 + 1)
        with self.assertRaises(PrometheusHTTPError):
            make_client(RecordingOpener(payload)).query_range("up", 0, 10)

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(PrometheusHTTPError, PrometheusError))
        self.assertTrue(issubclass(PrometheusResponseError, PrometheusError))
        self.assertTrue(issubclass(DuplicateSeriesError, PrometheusError))


class MissingSampleTests(unittest.TestCase):
    def _series(self, timestamps, name="up"):
        return MetricSeries(
            labels={"__name__": name},
            samples=tuple(MetricSample(float(t), 1.0) for t in timestamps),
        )

    def test_missing_samples_detected(self):
        series = self._series([1000, 1005, 1015])
        self.assertEqual(
            detect_missing_samples(series, 1000, 1015, step=5),
            (1010,),
        )

    def test_no_missing(self):
        series = self._series([1000, 1005, 1010])
        self.assertEqual(detect_missing_samples(series, 1000, 1010, step=5), ())

    def test_samples_outside_range_ignored(self):
        series = self._series([500, 2000, 1000, 1005])
        self.assertEqual(
            detect_missing_samples(series, 1000, 1010, step=5),
            (1010,),
        )

    def test_floating_tolerance(self):
        series = self._series([1000, 1005])
        series = MetricSeries(
            labels={"__name__": "up"},
            samples=(
                MetricSample(1000.0, 1.0),
                MetricSample(1005.0 + 5e-7, 1.0),
                MetricSample(1010.0, 1.0),
            ),
        )
        self.assertEqual(detect_missing_samples(series, 1000, 1010, step=5), ())

    def test_fixture_missing_samples(self):
        opener = RecordingOpener(load_fixture("prometheus-range-success.json"))
        series = make_client(opener).query_range("up", 1000, 1015)[0]
        self.assertEqual(
            detect_missing_samples(series, 1000, 1015, step=5),
            (1010,),
        )


class CounterResetTests(unittest.TestCase):
    def _series(self, values):
        return MetricSeries(
            labels={"__name__": "queries_total"},
            samples=tuple(
                MetricSample(float(1000 + 5 * i), float(v))
                for i, v in enumerate(values)
            ),
        )

    def test_single_reset(self):
        series = self._series([10, 20, 5, 15])
        resets = detect_counter_resets(series)
        self.assertEqual(len(resets), 1)
        reset = resets[0]
        self.assertIsInstance(reset, CounterReset)
        self.assertEqual(reset.previous_value, 20.0)
        self.assertEqual(reset.value, 5.0)
        self.assertEqual(reset.previous_timestamp, 1005.0)
        self.assertEqual(reset.timestamp, 1010.0)
        self.assertEqual(reset.identity, series.identity)

    def test_multiple_resets(self):
        series = self._series([10, 5, 20, 3])
        resets = detect_counter_resets(series)
        self.assertEqual(len(resets), 2)
        self.assertEqual([r.value for r in resets], [5.0, 3.0])
        self.assertLess(resets[0].timestamp, resets[1].timestamp)

    def test_equal_and_increasing_are_not_resets(self):
        series = self._series([10, 10, 15, 15, 20])
        self.assertEqual(detect_counter_resets(series), ())


# ---------------------------------------------------------------------------
# prometheus.yml configuration checks (stdlib only, no PyYAML)
# ---------------------------------------------------------------------------


def parse_simple_yaml(text: str):
    """Narrow deterministic parser for the restricted prometheus.yml subset."""

    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))

    import re

    mapping_re = re.compile(r"^([A-Za-z_][\w\-]*):(?:\s+(.*))?$")

    def parse_block(index, indent):
        is_list = lines[index][1].startswith("- ")
        container = [] if is_list else {}
        while index < len(lines):
            line_indent, content = lines[index]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise AssertionError(f"unexpected indent at: {content}")
            if is_list:
                if not content.startswith("- "):
                    break
                item = content[2:]
                match = mapping_re.match(item)
                if match is None:
                    container.append(_scalar(item))
                    index += 1
                    continue
                entry = {}
                index += 1
                if match.group(2):
                    entry[match.group(1)] = _scalar(match.group(2))
                else:
                    child_value, index = parse_block(index, lines[index][0])
                    entry[match.group(1)] = child_value
                while index < len(lines) and lines[index][0] > indent:
                    _, child = lines[index]
                    child_match = mapping_re.match(child)
                    if child_match is None:
                        break
                    index += 1
                    if child_match.group(2):
                        entry[child_match.group(1)] = _scalar(child_match.group(2))
                    else:
                        child_value, index = parse_block(index, lines[index][0])
                        entry[child_match.group(1)] = child_value
                container.append(entry)
            else:
                if content.startswith("- "):
                    break
                key, _, value = content.partition(":")
                index += 1
                if value.strip():
                    container[key.strip()] = _scalar(value)
                else:
                    if index < len(lines) and lines[index][0] > indent:
                        child_value, index = parse_block(index, lines[index][0])
                        container[key.strip()] = child_value
                    else:
                        container[key.strip()] = None
        return container, index

    result, _ = parse_block(0, lines[0][0])
    return result


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


class PrometheusConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CONFIG_PATH.read_text(encoding="utf-8")
        cls.config = parse_simple_yaml(cls.text)

    def test_global_intervals_five_seconds(self):
        self.assertEqual(self.config["global"]["scrape_interval"], "5s")
        self.assertEqual(self.config["global"]["evaluation_interval"], "5s")

    def test_external_label_placeholders(self):
        labels = self.config["global"]["external_labels"]
        self.assertEqual(labels["baseline_cycle_id"], "${A3_BASELINE_CYCLE_ID}")
        self.assertEqual(labels["manifest_id"], "${A3_MANIFEST_ID}")
        self.assertEqual(labels["run_id"], "${A3_RUN_ID}")

    def test_exact_three_jobs_with_targets(self):
        jobs = {job["job_name"]: job for job in self.config["scrape_configs"]}
        self.assertEqual(set(jobs), {"rathena", "node", "mariadb"})
        targets = {
            "rathena": "127.0.0.1:9468",
            "node": "127.0.0.1:9100",
            "mariadb": "127.0.0.1:9104",
        }
        for name, target in targets.items():
            job_targets = jobs[name]["static_configs"][0]["targets"]
            self.assertEqual(job_targets, [target])
            self.assertEqual(jobs[name]["scrape_interval"], "5s")
        self.assertEqual(jobs["rathena"]["metrics_path"], "/metrics")

    def test_no_remote_write_or_alertmanager(self):
        self.assertNotIn("remote_write", self.text)
        self.assertNotIn("alertmanager", self.text)

    def test_no_credentials_like_keys(self):
        lowered = self.text.lower()
        for marker in ("password", "token", "secret", "bearer", "authorization"):
            self.assertNotIn(marker, lowered)

    def test_no_public_bind_or_hardcoded_run_id(self):
        self.assertNotIn("0.0.0.0", self.text)
        # Only placeholders may appear as label values.
        self.assertNotIn("run-001", self.text)


if __name__ == "__main__":
    unittest.main()
