# rAthena Observability Sidecar

A dependency-free Python exporter that exposes baseline rAthena operational metrics in Prometheus text format. It is deliberately out-of-process, so enabling it does not alter login, character, map, combat, scripting, or persistence behavior.

## Metrics

- Process presence (`rathena_process_up`)
- CPU time, resident/virtual memory, threads, and open file descriptors
- Established TCP connections on login, character, and map ports
- Optional MariaDB probe success and duration
- Collector errors by reason

## Run

```bash
cp tools/observability/observer.example.json tools/observability/observer.json
python3 tools/observability/rathena_observer.py --config tools/observability/observer.json --check
python3 tools/observability/rathena_observer.py --config tools/observability/observer.json
curl http://127.0.0.1:9468/healthz
curl http://127.0.0.1:9468/metrics
```

The default listener is loopback-only. Put it behind an authenticated monitoring network or reverse proxy before exposing it outside the host.

## MariaDB probe

Enable the probe only after credentials are supplied securely through a MariaDB option file or environment-supported credential mechanism. Do not place database passwords in the JSON configuration or commit them to Git.

## Tests

```bash
python3 -m unittest discover -s tools/observability/tests -v
```

## Scope

This first version establishes a safe baseline for process, connection, and database reachability metrics. Map entity counts, packet counters, timer drift, script duration, and SQL call-site latency require small C++ hooks and are intentionally deferred until baseline measurements and CI are available.
