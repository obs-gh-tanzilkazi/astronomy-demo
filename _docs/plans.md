# Demo Enhancement Plans

Notes from planning session — June 10, 2026.

---

## Context

The astronomy demo deploys an EKS cluster running the OpenTelemetry demo app (~20 polyglot microservices) with the Observe agent collecting traces, metrics, and logs. The three signal types are all flowing to Observe. The goal is to add data correlation examples that are not currently configured.

---

## Planned Enhancements

### 1. Deployment events as correlation markers

**What:** When `helm upgrade` or `setup.py` runs a deploy step, emit an event into Observe that marks that point in time. This lets you visually correlate "latency went up at exactly this deploy."

**Why not configured today:** Nothing in `setup.py` or the Helm lifecycle emits a marker to Observe. Deploys are invisible on dashboards.

**Approach:**
- After each successful Helm install/upgrade in `setup.py`, POST an event to the Observe Events API
- Event should include: project name, step name (otel-demo / observe), timestamp, Helm release name, chart version
- Add a step in `setup.py` for this — emit on deploy and on teardown

**Files to modify:** `setup.py`, potentially `observe-values.yaml`

---

### 2. K8s events → service impact correlation

**What:** When a pod restarts, node pressure event fires, or OOMKill occurs, correlate that K8s event to downstream service error rate changes in Observe.

**Why not configured today:** K8s events are collected by the Observe agent (node-logs-metrics) but they aren't explicitly linked to the service-level RED metrics. The correlation exists in the raw data via shared resource attributes (pod name, namespace) but no derived dataset or worksheet surfaces it.

**Approach:**
- Verify K8s events are landing in Observe (query `Kubernetes Explorer/Kubernetes Logs` for event-type records)
- Build an Observe worksheet or dataset that joins K8s restart/OOMKill events with `Tracing/Service Metrics` error rate, correlated on `k8s.pod.name` + time window
- Add to runbook as a demo scenario: "trigger an OOMKill → show impact on service error rate"

**Datasets involved:**
- `Kubernetes Explorer/Kubernetes Logs` (ID: 43039006)
- `Tracing/Service Metrics` (ID: 43024580)

---

### 3. Locust flag change events

**What:** When a Locust load generator flag changes (e.g., user count, spawn rate, task weight), emit an event into Observe so load shape changes are visible on dashboards alongside telemetry.

**Why not configured today:** Locust flag changes have no telemetry footprint. A sudden change in request rate looks indistinguishable from a service issue.

**Approach:**
- Hook into Locust's event system (`events.init`, or a custom listener on flag/config changes)
- POST to Observe Events API on each flag change with: flag name, old value, new value, timestamp
- Alternatively, use Locust's built-in stats endpoint and poll for changes — emit events on detected deltas

**Files to create/modify:** A Locust plugin or listener script; wire it into the OTel demo Helm values if Locust config is managed there.

---

## Trace-to-Log Correlation — Validation Status

**Background:** Observe does trace-to-log correlation two ways:
1. **Resource-attribute correlation** (always on) — links signals by shared `service.name`, pod name, namespace + time window. Already working.
2. **Trace-ID correlation** (requires instrumentation) — exact span-scoped linkage via `trace_id` + `span_id` fields in log records. Only works if the service's logger is bridged to the OTel SDK.

**Expected gap:** Go and Rust services in the OTel demo likely use plain loggers (no OTel log bridge), so their log records won't have `trace_id` populated. Java (.NET have auto-instrumentation for logging. Python and Node.js vary.

**Validation needed:**
- Query `Kubernetes Explorer/Kubernetes Logs` (dataset ID: 43039006) grouped by `service.name`, count records where `trace_id` is not null vs null
- This will produce a map of which services have span-scoped correlation and which don't

**Status:** Query not yet run — blocked by Observe MCP SSE session initialization issue (see below).

---

## Observe MCP Server — Status & Known Issue

The `observe-mcp` binary is running at `http://127.0.0.1:8090/sse` and is registered in Cortex Code. One successful query was made (dataset listing). Subsequent calls fail with:

```
MCP error 0: method "tools/call" is invalid during session initialization
```

**Root cause:** `observe-mcp` uses the old MCP SSE protocol (client connects → receives session URL → must POST `initialize` → send `initialized` → then `tools/call`). Cortex Code's MCP client hits a race condition on reconnect — it sends `tools/call` before initialization completes.

**Workarounds to try:**
1. Check `~/observe-mcp --help` for an HTTP transport flag (stateless HTTP would not have this problem)
2. Keep the SSE connection alive to avoid reconnects
3. Query the Observe REST API directly using the token in `config.env`

**Datasets confirmed available:**
| Dataset | ID |
|---|---|
| Kubernetes Explorer/Kubernetes Logs | 43039006 |
| Kubernetes Explorer/Prometheus Metrics | 43036782 |
| Tracing/Service Metrics | 43024580 |
| Tracing/Service Explorer Spans | 43024578 |
| Tracing/Span | 43024574 |
| Metrics/OpenTelemetry | 43024573 |
| AWS-Quickstart/CloudTrail Events | 43036769 |
| AWS-Quickstart/AWS Asset Inventory | 43036768 |
| AWS-Quickstart/Logs | 43036767 |
| usage/Observe Usage Metrics | 43024529 |
