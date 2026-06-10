# Skill: Self-Contained Observe Demo Environments

A codified skill for creating, operating, and tearing down isolated demo environments that showcase the Observe observability platform against realistic microservice workloads.

---

## Purpose

Deploy a fully instrumented microservice application on Kubernetes with the Observe agent collecting traces, metrics, and logs — ready for live demos, customer POCs, and internal training. Each environment is self-contained, reproducible, and disposable.

---

## Principles

### 1. Single source of truth for configuration

All environment-specific values live in one file (`config.env`). No value should be hardcoded in more than one place. Infrastructure code, deploy scripts, and documentation all read from or reference this file.

```
AWS_DEFAULT_REGION=ap-southeast-2
OBSERVE_TOKEN=<customerid>:<token>
OBSERVE_ENDPOINT=https://<customer-id>.collect.observeinc.com/
PROJECT_NAME=demo-acme
```

### 2. Idempotency everywhere

Every operation must be safe to run multiple times:
- `helm upgrade --install` (never bare `helm install`)
- `kubectl create namespace ... || true`
- `helm repo add --force-update`
- Terraform only changes what's missing (it tracks what already exists in its state file)

A demo that fails halfway through a deploy must be recoverable by re-running the same command.

### 3. Preflight validation

Before touching cloud resources, verify:
- All required CLI tools are on PATH (terraform, aws, kubectl, helm, curl)
- All required config values are set (fail fast with actionable error messages)
- Helm repos are added and updated
- AWS credentials are valid (`aws sts get-caller-identity`)

### 4. Isolation by design

Each demo gets:
- Its own Terraform workspace
- Its own EKS cluster (full blast radius isolation)
- Its own K8s namespace for the application
- A fixed `observe` namespace for the agent (chart requirement)
- Tagged resources for cost attribution and cleanup

### 5. Dynamic infrastructure discovery

Never hardcode cloud-provider-specific values that can be discovered:
- Availability zones: `data "aws_availability_zones" { state = "available" }`
- Public IP: detected at runtime via external service
- AMI IDs: resolved via SSM parameter lookup (handled by EKS module)

### 6. Clean teardown with no orphans

Teardown must be complete and leave no billable resources:
- Reverse-order removal (app before infra)
- CloudWatch log group cleanup
- kubeconfig entry removal
- Terraform workspace deletion
- EC2 instance verification (catch orphans from failed deploys)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Load Generator (Locust)                                    │
│  Simulates realistic user traffic                           │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Frontend Proxy (Envoy)                                     │
│  API gateway / reverse proxy                                │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Microservices (10-20 services)                             │
│  Instrumented with OpenTelemetry SDKs                       │
│  Languages: Go, Java, .NET, Python, Node.js, Rust           │
└──────────────────────────┬──────────────────────────────────┘
                           │ OTLP
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  OTel Collector (DaemonSet)                                 │
│  Receives app telemetry, exports to:                        │
│   → Internal backends (Jaeger, Prometheus, OpenSearch)       │
│   → Observe forwarder (otlp/observe exporter)               │
└──────────────────────────┬──────────────────────────────────┘
                           │ OTLP gRPC :4317
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Observe Agent (namespace: observe)                         │
│   • Forwarder — receives app OTLP, forwards to Observe     │
│   • Node-logs-metrics — collects K8s logs, metrics, events  │
│   • Cluster-metrics — K8s resource state                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Observe Platform (SaaS)                                    │
│  Traces, metrics, logs, K8s resource state — all correlated │
└─────────────────────────────────────────────────────────────┘
```

---

## Deployment Steps

### Step 1: Infrastructure (VPC + EKS)

| Concern | Approach |
|---------|----------|
| Networking | VPC with 2 private + 2 public subnets, NAT gateway |
| Cluster | EKS with managed node group, public + private endpoints |
| Access control | Endpoint restricted to deployer's IP via security group |
| Node access | SSM enabled (no SSH keys needed) |
| IMDS | Hop limit = 2 (required for OTel resource detection) |
| Add-ons | vpc-cni (before_compute=true), kube-proxy, coredns |

### Step 2: Application

| Concern | Approach |
|---------|----------|
| App source | OpenTelemetry demo Helm chart (realistic polyglot microservices) |
| Namespace | Project-specific namespace for isolation |
| Overrides | Custom values YAML to wire telemetry to Observe |
| Resource limits | Tune memory for .NET services (OOMKill prevention) |

### Step 3: Observe Agent

| Concern | Approach |
|---------|----------|
| Namespace | Fixed `observe` (chart requirement) |
| Authentication | Token in K8s secret, injected via env var |
| Port conflicts | Disable hostPort on forwarder (use ClusterIP service) |
| Resource sizing | CPU request 150m (fits alongside app on t3.large nodes) |
| Helm idempotency | `helm upgrade --install` always |

### Step 4: Telemetry Pipeline Wiring

The app's OTel collector must explicitly export to the Observe forwarder. This is NOT automatic.

```yaml
exporters:
  otlp/observe:
    endpoint: observe-agent-forwarder.observe.svc.cluster.local:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      exporters: [otlp/jaeger, otlp/observe]
    metrics:
      exporters: [otlphttp/prometheus, otlp/observe]
    logs:
      exporters: [opensearch, otlp/observe]
```

When overriding pipelines in Helm, you MUST repeat ALL existing receivers/processors/exporters. Helm replaces lists — it does not merge them.

---

## Critical Gotchas

### Observe chart namespace

The Observe agent chart hardcodes ClusterRole references to the `observe` namespace. Custom namespaces break the install. Always use `observe`.

### Stale ClusterRoles after failed installs

Cluster-scoped resources survive namespace deletion. A failed `helm install` leaves orphaned ClusterRoles that block re-install. Fix: find and delete resources annotated with the old release name.

### Token quoting

`config.env` values must NOT be wrapped in quotes. Quoted tokens produce 401 errors because literal `"` characters end up in the K8s secret.

### Host port conflicts

If the app runs its own OTel collector DaemonSet with hostPort bindings, the Observe forwarder cannot bind the same ports. Disable hostPort on the forwarder and use ClusterIP service routing instead.

### CPU accounting

t3.large nodes have ~1930m allocatable CPU (not 2000m). Always check actual allocatable resources before sizing requests.

### IMDSv2 hop limit

EKS nodes default to hop_limit=1 which blocks pods from reaching EC2 instance metadata. The Observe agent's resource detection processor requires IMDS access. Set hop_limit=2.

### Health endpoint binding

The OTel health extension binds to `${MY_POD_IP}`, not localhost. Always use the pod IP for health checks.

---

## Operational Patterns

### Status check

```bash
python3 setup.py --project <name> --status
```

Verifies: Terraform state, EKS cluster health, node readiness, pod status across all namespaces.

### Reconnect after IP change

When your public IP changes (VPN, laptop sleep), the EKS API endpoint security group blocks kubectl. Re-run deploy to update:

```bash
python3 setup.py --project <name> --deploy --step vpc
```

### Validate Observe data flow

```bash
# 1. Agent health
kubectl exec -n observe <pod> -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'

# 2. Check for export errors
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --tail=100 \
  | grep -iE "error|fail|401|500|timeout"

# 3. Verify token
kubectl get secret agent-credentials -n observe \
  -o jsonpath='{.data.OBSERVE_TOKEN}' | base64 -d

# 4. Test internal connectivity to forwarder
kubectl run curl-test --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s -o /dev/null -w "%{http_code}" \
  http://observe-agent-forwarder.observe.svc.cluster.local:4317
```

### Staged deployment for reliability

Deploy infrastructure in two phases to catch failures early:
1. `terraform apply -target=module.vpc` — verify networking
2. `terraform apply` — deploy EKS (takes ~15-20 min)

### Cost control

- Use `t3.large` (2 nodes minimum for HA)
- Single NAT gateway (not per-AZ) for demos
- Tear down environments when not in active use
- CloudWatch log groups accumulate cost — always clean up on teardown

---

## File Structure

```
<demo-name>/
├── setup.py                   # Orchestration script (deploy/teardown/status)
├── config.env                 # All environment-specific values (gitignored)
├── config.env.example         # Template with placeholder values (committed)
├── main.tf                    # VPC + EKS infrastructure
├── variables.tf               # Terraform input variables
├── observe-values.yaml        # Observe agent Helm overrides
├── otel-demo-override.yaml    # App Helm overrides (wires telemetry to Observe)
└── _docs/
    ├── runbook.md             # Operational runbook
    ├── learnings.md           # Troubleshooting history (append-only)
    └── diagnostics.md         # Manual commands & gotchas
```

---

## Checklist: New Demo Environment

- [ ] `config.env` created with all required values (region, token, endpoint)
- [ ] Preflight passes (all tools installed, AWS credentials valid)
- [ ] Infrastructure deployed (VPC + EKS healthy, nodes Ready)
- [ ] Application deployed (all pods Running, frontend accessible)
- [ ] Observe agent deployed (all pods Running, no export errors)
- [ ] Telemetry pipeline verified (traces + metrics + logs visible in Observe UI)
- [ ] Load generator running (Locust producing realistic traffic patterns)
- [ ] Teardown tested (full teardown leaves no orphaned resources)

---

## Checklist: Code Quality for Deploy Scripts

- [ ] No hardcoded regions, AZs, or tenant-specific values
- [ ] All file paths quoted (handles spaces in directory names)
- [ ] `helm upgrade --install` (never bare `helm install`)
- [ ] Preflight validation for all required tools and config
- [ ] Helm repos added idempotently at script start
- [ ] `OBSERVE_ENDPOINT` required (no tenant-specific fallback defaults)
- [ ] No dead code or no-op string replacements
- [ ] Documentation matches current code behaviour
- [ ] Secrets never committed (config.env in .gitignore)
- [ ] All subprocess commands use `shell=True` only when necessary (prefer arg lists for user input)

---

## EKS Module v21 Requirements

When using `terraform-aws-modules/eks/aws ~> 21.0`:

| Setting | Required | Why |
|---------|----------|-----|
| `enable_cluster_creator_admin_permissions = true` | Yes | Without this, deployer has no cluster access |
| `addons.vpc-cni.before_compute = true` | Yes | Prevents chicken-and-egg deadlock with node group |
| `addons.kube-proxy` | Yes | No longer auto-installed |
| `addons.coredns` | Yes | No longer auto-installed |
| `metadata_options.http_put_response_hop_limit = 2` | Yes | Required for IMDS access from pods |

---

## Extending to New Demo Scenarios

To add a new demo workload (different from the OTel astronomy shop):

1. Create a new Helm values override that wires the app's telemetry to `observe-agent-forwarder.observe.svc.cluster.local:4317`
2. Add a new step in `setup.py` or create a separate script
3. Ensure the app's OTel collector does not claim hostPorts that conflict with the Observe forwarder
4. Size node resources to fit both the app and the Observe agent
5. Document any app-specific gotchas in `_docs/learnings.md`
6. Test full deploy → verify in Observe UI → full teardown cycle before using in a live demo
