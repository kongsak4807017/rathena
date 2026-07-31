# rAthena Observability V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, dependency-free Prometheus exporter for rAthena process, TCP connection, and optional MariaDB reachability metrics without changing gameplay behavior.

**Architecture:** Run a Python sidecar on the same Linux host as rAthena. It reads Linux `/proc`, optionally executes a bounded MariaDB probe, stores the latest snapshot in memory, and serves `/metrics` and `/healthz` over a loopback HTTP listener.

**Tech Stack:** Python 3 standard library, Linux `/proc`, Prometheus text exposition, GitHub Actions, `unittest`.

## Global Constraints

- No modifications to login, character, map, combat, script, packet, or persistence behavior.
- No third-party Python dependencies.
- Listener defaults to `127.0.0.1`.
- MariaDB probing is disabled by default and bounded by a timeout.
- Database credentials must not be stored in repository configuration.

---

### Task 1: Configuration and collection primitives

**Files:**
- Create: `tools/observability/rathena_observer.py`
- Test: `tools/observability/tests/test_rathena_observer.py`

- [x] Write failing tests for configuration parsing and TCP state counting.
- [x] Implement JSON configuration parsing, process discovery, `/proc` process sampling, TCP established connection counting, and optional MariaDB probe.
- [x] Run tests and verify they pass.

### Task 2: Prometheus rendering and HTTP service

**Files:**
- Modify: `tools/observability/rathena_observer.py`
- Test: `tools/observability/tests/test_rathena_observer.py`

- [x] Write a failing test for Prometheus rendering.
- [x] Implement deterministic labels, metric metadata, snapshot storage, `/metrics`, and `/healthz`.
- [x] Run tests and verify they pass.

### Task 3: Configuration, documentation, and CI

**Files:**
- Create: `tools/observability/observer.example.json`
- Create: `tools/observability/README.md`
- Create: `docs/observability/OBSERVABILITY_V1.md`
- Create: `.github/workflows/observability-tests.yml`

- [x] Add a safe loopback-only example configuration.
- [x] Document deployment, security, metrics, dashboards, and next core instrumentation gates.
- [x] Add GitHub Actions coverage for Python 3.11 and 3.12.
