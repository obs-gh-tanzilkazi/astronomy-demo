# EKS Deployment Troubleshooting Learnings

_Last updated: 30 April 2026_

---

### 11. App telemetry (traces, metrics, logs) not reaching Observe — otel-collector pipeline isolation
**Symptom**
Observe UI shows K8s cluster metrics and pod logs but zero application traces, app-level
metrics, or structured app logs from the astronomy-demo services.

**Cause**
The astronomy-demo app has its own internal telemetry pipeline that is completely isolated
from Observe:
```
App services → otel-collector-agent (DaemonSet) → Jaeger (traces)
                                                 → Prometheus (metrics)
                                                 → OpenSearch (logs)
```
The Observe forwarder DaemonSet runs in parallel but nothing sends telemetry to it.
Observe only receives what its own agents collect (K8s events, pod logs via file tail,
K8s resource metrics) — not the application-instrumented OTLP data.

**Fix**
Override the otel-demo Helm chart to add `otlp/observe` as an additional exporter on all
three pipelines. Existing destinations (Jaeger, Prometheus, OpenSearch) are unchanged.

Create `otel-demo-override.yaml`:
```yaml
opentelemetry-collector:
  config:
    exporters:
      otlp/observe:
        endpoint: http://observe-agent-forwarder.observe.svc.cluster.local:4317
        tls:
          insecure: true
    service:
      pipelines:
        traces:
          processors: [memory_limiter, resourcedetection, resource, transform, batch]
          exporters: [otlp/jaeger, debug, spanmetrics, otlp/observe]
        metrics:
          receivers: [otlp, kafkametrics, spanmetrics]
          processors: [memory_limiter, resourcedetection, resource, batch]
          exporters: [otlphttp/prometheus, debug, otlp/observe]
        logs:
          processors: [memory_limiter, resourcedetection, resource, batch]
          exporters: [opensearch, debug, otlp/observe]
```

Apply:
```bash
helm upgrade my-otel-demo open-telemetry/opentelemetry-demo -n default -f otel-demo-override.yaml
```

**Important**: When overriding `service.pipelines`, you must repeat ALL existing
processors/receivers/exporters. Helm replaces lists entirely — it does not merge them.

**Prevention**
When deploying any observability agent alongside an existing app that has its own
telemetry pipeline, explicitly wire the two together. The Observe agent does not
automatically intercept telemetry going to other collectors.

---

### 10. Observe agent export errors — HTTP 500 and context deadline exceeded
**Symptom**
```
error: Exporting failed. Dropping data.
  otlphttp/observe/base → HTTP 500 from https://<tenant>.collect.observeinc.com/v2/otel/v1/logs
  prometheusremotewrite/observe → context deadline exceeded (metrics)
```
Data dropped, not appearing in Observe UI. Errors repeat every ~60 seconds.

**Cause**
Two separate failures:
- **HTTP 500 (logs)**: Observe's server rejected the payload — typically caused by a
  malformed request, oversized batch, or a transient backend issue.
- **Context deadline exceeded (metrics)**: The Prometheus Remote Write export request
  timed out — can be caused by slow endpoint response, network latency, or incorrect
  Prometheus Remote Write endpoint URL.

Both are `Permanent error: Dropping data` — the agent exhausts its retry queue and stops
retrying those specific batches. New data collected after the queue drains is sent fresh
and may succeed.

**Diagnosis**
```bash
# Check for export errors (filter out prometheus scrape noise)
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --tail=500 \
  | grep -v "scrape" \
  | grep -iE "error|fail|500|timeout"

# Check if errors are current or historical
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --since=5m \
  | grep -iE "error|fail|500|timeout"
```

**Recovery**
The agent self-recovers after draining the retry queue. Verify agent health:
```bash
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=node-logs-metrics -o name | head -1) \
  -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'
# Expect: {"status":"Server available", ...}
```

**Note on health endpoint**: The agent binds its health check to `${MY_POD_IP}` (pod eth0),
not `localhost`. Using `localhost:13133` always returns `Connection refused`. Always use
`${MY_POD_IP}:13133` or the pod's IP directly.

---

### 9. Observe relay config — empty `token:` field is intentional
**Symptom**
```bash
kubectl get configmap observe-agent -n observe -o yaml | grep token
# shows:  token:
# (blank)
```
Concern that the Observe agent is sending unauthenticated requests.

**Cause / Expected behaviour**
The `observe-agent.yaml` (relay config) has `token: ` empty by design. The agent reads
the token from the `TOKEN` environment variable, which is injected from the
`agent-credentials` Kubernetes secret. The empty config field is intentional — the env
var takes precedence.

**Verify**
```bash
# Confirm TOKEN env var is set inside the pod
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=cluster-metrics -o name | head -1) \
  -- env | grep TOKEN
# Expect: TOKEN=<customerid>:<token>

# Confirm secret has the right key (show first 8 chars only)
kubectl get secret agent-credentials -n observe \
  -o jsonpath='{.data}' | python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
[print(k, '=', base64.b64decode(v).decode()[:8]+'...') for k,v in d.items()]
"
```

---

### 8. Observe forwarder DaemonSet Pending — Insufficient CPU (node allocatable ≠ 2000m)
**Symptom**
```
0/2 nodes are available: 1 Insufficient cpu, 1 node(s) didn't satisfy plugin(s) [NodeAffinity]
```
One forwarder pod stuck Pending even after reducing CPU request from the chart default of 300m.

**Cause**
`t3.large` has 2 vCPUs but Kubernetes reserves ~70m for the kubelet and system processes.
Actual allocatable CPU is **1930m**, not 2000m. One node had 1750m already requested by
the astronomy-demo app's services, leaving only **180m free**. The forwarder's 200m request
(already reduced from 300m) still didn't fit.

```
Allocatable:  1930m
Requested:    1750m  (astronomy-demo otel-collector + observe node-logs-metrics + kube-system)
Free:          180m
Forwarder:    200m  → doesn't fit
```

**Fix**
Reduce the forwarder CPU request to 150m in `observe-values.yaml`. No CPU limit is set so
the pod can still burst freely when the node has spare capacity:
```yaml
forwarder:
  resources:
    requests:
      cpu: 150m
      memory: 512Mi
    limits:
      memory: 512Mi
```

**Diagnosis commands**
```bash
# Actual allocatable CPU per node (not the EC2 spec)
kubectl get nodes -o custom-columns="NAME:.metadata.name,ALLOC_CPU:.status.allocatable.cpu"

# CPU/memory already requested on each node
kubectl describe nodes | grep -A 8 "Allocated resources"

# Confirm a pod's actual resource request
kubectl get pod -n <ns> <pod> -o jsonpath='{.spec.containers[0].resources}'
```

**Prevention**
Always check `kubectl describe nodes | grep -A 8 "Allocated resources"` before sizing
requests. For t3.large: effective allocatable CPU is ~1930m, not 2000m.

---

### 7. Observe forwarder DaemonSet Pending — host port conflict with otel-collector-agent
**Symptom**
```
0/2 nodes are available: 1 node(s) didn't have free ports for the requested pod ports,
1 node(s) didn't satisfy plugin(s) [NodeAffinity]
```
Both forwarder DaemonSet pods stuck `Pending` with `NODE: <none>` for 14+ hours.

**Cause**
The astronomy-demo Helm chart deploys its own `otel-collector-agent` DaemonSet that claims
the exact same host ports on every node:

| Port | Protocol | Purpose |
|------|----------|---------|
| 4317 | TCP | OTLP gRPC |
| 4318 | TCP | OTLP HTTP |
| 6831 | UDP | Jaeger compact |
| 14250 | TCP | Jaeger gRPC |
| 14268 | TCP | Jaeger thrift |
| 9411 | TCP | Zipkin |

The Observe forwarder DaemonSet also claims these as `hostPort` (because
`node.forwarder.traces.enabled: true` is the chart default). Two DaemonSets cannot bind
the same host port on the same node — one pod schedules, the other is permanently blocked.

**Diagnosis**
```bash
# Find every pod with a host port claim across the cluster
kubectl get pods --all-namespaces -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data['items']:
    for c in item['spec'].get('containers', []):
        for p in c.get('ports', []):
            if p.get('hostPort', 0) > 0:
                print(item['metadata']['namespace'], item['metadata']['name'],
                      item['spec'].get('nodeName','<none>'), c['name'], p['hostPort'])
"

# Confirm a pending pod's actual injected nodeAffinity
kubectl get pod -n observe <pod> -o jsonpath='{.spec.affinity}' | python3 -m json.tool
```

**Fix**
Disable host ports on the Observe forwarder in `observe-values.yaml`. The forwarder still
listens on these `containerPort`s and is reachable via its ClusterIP service:
```yaml
forwarder:
  ports:
    otlp:
      enabled: true
      containerPort: 4317
      servicePort: 4317
      hostPort: 0      # 0 = no host port binding
      protocol: TCP
    # repeat for otlp-http, jaeger-compact, jaeger-grpc, jaeger-thrift, zipkin
```

Apply with:
```bash
helm upgrade observe-agent observe/agent -n observe -f observe-values.yaml
```

**Prevention**
When deploying the Observe agent alongside any app that runs its own OTel collector
DaemonSet (e.g. opentelemetry-demo), always disable host ports on the Observe forwarder.
Apps should send telemetry to the forwarder's ClusterIP service
(`observe-agent-forwarder.observe.svc.cluster.local:4317`) rather than localhost host ports.

---

### 6. Observe agent CrashLoopBackOff — EC2 IMDSv2 hop limit
**Symptom**
```
Error: cannot start pipelines: failed to start "resourcedetection/cloud" processor:
can't get K8s Instance Metadata; node name is empty
```
Pod status: `CrashLoopBackOff`, init container exits cleanly, main container crashes immediately.

**Cause**
EKS nodes default to `httpPutResponseHopLimit=1` on EC2 IMDS. This prevents pods from
reaching the instance metadata service at `169.254.169.254`. The Observe agent's
`resourcedetection/cloud` processor queries IMDS to detect cloud resource attributes
(instance ID, node name, region etc). With hop limit 1, the request never reaches IMDS
— the processor fails to start and crashes the agent.

**Fix**
Set `http_put_response_hop_limit = 2` in the node group launch template:
```hcl
eks_managed_node_groups = {
  default = {
    metadata_options = {
      http_endpoint               = "enabled"
      http_tokens                 = "required"
      http_put_response_hop_limit = 2
    }
  }
}
```
This requires a rolling node replacement (`terraform apply`).

**Prevention**
Any workload that uses cloud resource detection (OpenTelemetry collectors, Observe agent,
Datadog agent, etc.) requires `hop_limit = 2`. Set it by default on EKS clusters running
telemetry agents.

---

## Context
Deploying an EKS 1.35 cluster (`astronomy-demo`) in `ap-southeast-2` using
`terraform-aws-modules/eks/aws ~> 21.0`.

---

## Issues Encountered & Root Causes

### 1. Kubernetes version 1.36 has no AMI
**Symptom**
```
Error: reading SSM Parameter (/aws/service/eks/optimized-ami/1.36/...): couldn't find resource
```
**Cause**
EKS 1.36 AMI was not yet published in `ap-southeast-2`. The module looks up the
node AMI via SSM at plan time — it fails immediately rather than at deploy time.

**Fix**
Downgrade to `kubernetes_version = "1.35"` (confirmed available via targeted plan).

**Prevention**
Before bumping the Kubernetes version, verify the AMI exists:
```bash
terraform plan -target='module.eks.module.eks_managed_node_group["default"].data.aws_ssm_parameter.ami[0]'
```

---

### 2. Missing EKS cluster creator admin permissions
**Symptom**
```
error: You must be logged in to the server (the server has asked for the client to provide credentials)
```
**Cause**
EKS module v21 switched from the legacy `aws-auth` ConfigMap to the EKS Access
Entries API. Unlike the old approach, this requires explicitly opting in to grant
the cluster creator admin access. Without this flag, the IAM identity that ran
Terraform has no access to the cluster.

**Fix**
```hcl
module "eks" {
  enable_cluster_creator_admin_permissions = true
}
```

**Prevention**
Always include this flag when using `terraform-aws-modules/eks/aws ~> 21.0`.

---

### 3. EKS core add-ons not installed
**Symptom**
```
NodeCreationFailure: Unhealthy nodes in the kubernetes cluster
```
```
kubectl get pods -n kube-system → No resources found
```
**Cause**
EKS module v21 no longer auto-installs VPC CNI, kube-proxy, and CoreDNS as
self-managed DaemonSets. These must be explicitly declared as managed add-ons.
Without `vpc-cni`, nodes have no pod networking and can never become `Ready`.

**Fix**
```hcl
module "eks" {
  addons = {
    vpc-cni    = { before_compute = true }
    kube-proxy = {}
    coredns    = {}
  }
}
```

**Prevention**
Always declare the three core add-ons when using EKS module v21+.

---

### 4. vpc-cni chicken-and-egg deadlock
**Symptom**
```
timeout while waiting for state to become 'ACTIVE' (last state: 'CREATING', timeout: 15m0s)
```
Nodes stuck `NotReady` indefinitely. `aws eks list-addons` returns empty.

**Cause**
With `before_compute = false` (the default), Terraform creates add-ons *after*
the node group. But the node group waits for nodes to become `Ready`, which
requires `vpc-cni` to be running — which hasn't been deployed yet. Deadlock.

```
Node group creation → waits for nodes Ready
                           ↓
                      nodes need vpc-cni
                           ↓
                  vpc-cni not deployed yet
                  (waits for node group ACTIVE)
                           ↓
                         STUCK
```

**Fix**
Set `before_compute = true` on `vpc-cni` so it is deployed before the node group:
```hcl
addons = {
  vpc-cni    = { before_compute = true }  # deploys before node group
  kube-proxy = {}                          # deploys after
  coredns    = {}                          # deploys after
}
```

**Prevention**
Always set `before_compute = true` on `vpc-cni`. The other add-ons do not need it.

---

### 5. Orphaned EC2 instances from failed deployments
**Cause**
Each failed node group attempt left EC2 instances running. The node group was
rolled back by AWS/Terraform but the underlying instances were not always
terminated promptly, accumulating cost and polluting `kubectl get nodes`.

**Cleanup**
```bash
aws ec2 describe-instances \
  --region ap-southeast-2 \
  --filters "Name=tag:eks:cluster-name,Values=astronomy-demo" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text | xargs aws ec2 terminate-instances \
    --region ap-southeast-2 --instance-ids
```

---

## Module v21 Migration Notes

| Behaviour | v19/v20 | v21 |
|-----------|---------|-----|
| Cluster creator access | Auto-granted via `aws-auth` | Requires `enable_cluster_creator_admin_permissions = true` |
| VPC CNI | Auto-installed as self-managed DaemonSet | Must declare in `addons` with `before_compute = true` |
| kube-proxy | Auto-installed | Must declare in `addons` |
| CoreDNS | Auto-installed | Must declare in `addons` |
| Node auth | `aws-auth` ConfigMap | EKS Access Entries API |
| Logging attribute | `cluster_enabled_log_types` | `enabled_log_types` |
| Add-ons attribute | `cluster_addons` | `addons` |

---

## Final Working Configuration Summary

```hcl
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = "astronomy-demo"
  kubernetes_version = "1.35"

  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = [var.my_ip_cidr]
  enable_irsa                  = true

  enable_cluster_creator_admin_permissions = true

  enabled_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  addons = {
    vpc-cni    = { before_compute = true }
    kube-proxy = {}
    coredns    = {}
  }

  eks_managed_node_groups = {
    default = {
      instance_types                        = ["t3.medium"]
      min_size                              = 2
      max_size                              = 2
      desired_size                          = 2
      attach_cluster_primary_security_group = true

      timeouts = {
        create = "30m"
        update = "30m"
        delete = "30m"
      }

      iam_role_additional_policies = {
        AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }
    }
  }
}
```

---

## Staged Deployment Process (`deploy.sh`)

Always deploy in two stages to catch failures early:
1. `terraform apply -target=module.vpc` — verify networking before touching EKS
2. `terraform apply` — deploy remaining resources
