# rAthena Observability V1

## Objective

Establish measurable production baselines before changing rAthena gameplay code. The first release is an out-of-process Prometheus exporter committed inside the rAthena repository.

## Operational dashboard

Recommended first panels:

1. `rathena_process_up` by server process
2. Resident memory and open file descriptors by process
3. Established connections by ports 6900, 6121, and 5121
4. MariaDB probe success and latency
5. Observer collection errors

## Alert starting points

These thresholds are initial guardrails and must be calibrated with load tests:

- `rathena_process_up == 0` for 30 seconds: critical
- MariaDB probe failure for 30 seconds: critical
- Open file descriptors above 80% of the service limit: warning
- Process resident memory rising continuously for 30 minutes: investigate
- Established map connections dropping abruptly while login connections remain stable: investigate gateway/map-server failure

## Next instrumentation gates

Core C++ instrumentation should be added only after this exporter is deployed and the following are known:

- Normal process CPU and memory at idle, 100, 500, and 1,000 clients
- Normal TCP connection counts and churn
- MariaDB probe p50/p95/p99
- Whether process or database saturation appears before gameplay latency

The next core patch will add map entity gauges, timer drift histograms, packet counters, slow script observations, and SQL call-site latency behind a disabled-by-default configuration flag.
