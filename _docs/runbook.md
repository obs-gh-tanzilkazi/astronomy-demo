# Astronomy Demo — Runbook

Full setup and teardown of the `astronomy-demo` EKS cluster, OpenTelemetry demo app,
and Observe monitoring agent in AWS `ap-southeast-2`.

**Known-good chart versions** (pinned in all helm commands below):

| Chart | Version | App version |
|-------|---------|-------------|
| `observe/agent` | `0.86.1` | `2.15.0` |
| `open-telemetry/opentelemetry-demo` | `0.40.7` | `2.2.0` |

---

## Prerequisites

### Tools required

```bash
# Verify all tools are installed
terraform version        # >= 1.5.0
aws --version            # >= 2.x
kubectl version --client
helm version             # >= 3.x
```

Install missing tools:
```bash
brew install terraform awscli kubectl helm
```

### AWS access

```bash
# Option A — IAM user access keys
aws configure
# Enter: Access Key ID, Secret Access Key, region: ap-southeast-2, output: json

# Option B — SSO (if Identity Center is configured)
aws sso login --profile <your-profile>

# Verify access
aws sts get-caller-identity
```

### Helm chart repositories

```bash
helm repo add observe          https://observeinc.github.io/helm-charts
helm repo add open-telemetry   https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
```

---

## Project structure

```
astronomy-demo/
├── main.tf                    # VPC + EKS infrastructure
├── variables.tf               # Input variables
├── terraform.tfvars           # Your IP CIDR — auto-written by deploy.sh
├── deploy.sh                  # Staged deploy script (infrastructure only)
├── observe-values.yaml        # Observe agent Helm overrides
├── otel-demo-override.yaml    # otel-demo Helm overrides (adds Observe exporter)
└── _docs/
    ├── runbook.md             # This file
    ├── learnings.md           # Troubleshooting history
    └── diagnostics.md         # Quick-reference diagnostic commands
```

---

## Part 1 — Infrastructure (Terraform)

### Step 1 — Update your IP

`deploy.sh` detects your public IP automatically and writes it to `terraform.tfvars`
before each deploy. No manual editing required.

If running Terraform manually (not via `deploy.sh`), update the IP yourself first:

```bash
echo "my_ip_cidr = \"$(curl -s https://checkip.amazonaws.com)/32\"" > terraform.tfvars
```

### Step 2 — Initialise Terraform

```bash
terraform init
```

> Run this every time after a fresh clone or after `terraform destroy`.

### Step 3 — Validate config

```bash
terraform validate
# Expect: "Success! The configuration is valid."
# One deprecation warning in the VPC module is expected and not a blocker.
```

### Step 4 — Deploy (staged)

Always deploy in two stages. This catches VPC issues before spending 20+ minutes
waiting for EKS to fail.

```bash
# Option A — use the deploy script (interactive, recommended)
bash deploy.sh

# Option B — manual stages
terraform apply -target=module.vpc
# Verify in AWS console: VPC, 2 private subnets, 2 public subnets, NAT gateway
terraform apply
```

> **Expected duration**: VPC ~2 min, EKS ~15–20 min.

### Step 5 — Configure kubectl

```bash
aws eks update-kubeconfig --region ap-southeast-2 --name astronomy-demo
```

### Step 6 — Verify cluster health

```bash
# Nodes — expect 2x Ready
kubectl get nodes -o wide

# Core system pods — expect vpc-cni, kube-proxy, coredns all Running
kubectl get pods -n kube-system

# EKS managed add-ons
aws eks list-addons --cluster-name astronomy-demo --region ap-southeast-2
# Expect: vpc-cni, kube-proxy, coredns

# Quick DNS smoke test
kubectl run smoke-test --image=busybox --restart=Never --rm -it \
  -- nslookup kubernetes.default
kubectl delete pod smoke-test --ignore-not-found
```

---

## Part 2 — OpenTelemetry Demo App

### Step 7 — Deploy astronomy-demo

The `otel-demo-override.yaml` is applied here so the app forwards telemetry to
the Observe agent from the moment it starts.

```bash
helm upgrade --install my-otel-demo open-telemetry/opentelemetry-demo \
  --version 0.40.7 \
  --namespace default \
  --create-namespace \
  -f otel-demo-override.yaml \
  --timeout 10m \
  --wait
```

> `--wait` blocks until all pods reach Running/Ready. Expected ~5 min.

### Step 8 — Verify app pods

```bash
kubectl get pods -n default
# Expect ~20 pods all Running or Completed
```

### Step 9 — Access the frontend

```bash
# Port-forward in the background
kubectl port-forward svc/frontend-proxy 8080:8080 -n default &

# Open in browser
open http://localhost:8080

# Stop port-forward when done
kill %1
```

---

## Part 3 — Observe Monitoring Agent

### Step 10 — Add the Observe Helm repo

```bash
helm repo add observe https://observeinc.github.io/helm-charts
helm repo update
```

### Step 11 — Create the observe namespace and secret

Get your Observe ingest token from the Observe UI:
- Log into your Observe tenant → **Datastream → Kubernetes** → **Create ingest token**
- Copy the token (format: `<customerid>:<token>`)

```bash
kubectl create namespace observe

kubectl -n observe create secret generic agent-credentials \
  --from-literal=OBSERVE_TOKEN='<your-observe-token>'

kubectl annotate secret agent-credentials -n observe \
  meta.helm.sh/release-name=observe-agent \
  meta.helm.sh/release-namespace=observe

kubectl label secret agent-credentials -n observe \
  app.kubernetes.io/managed-by=Helm
```

> The annotations and label allow Helm to manage the secret during upgrades.
> The secret is **not** deleted by `helm uninstall` — delete it manually during teardown.

### Step 12 — Deploy the Observe agent

`observe-values.yaml` contains all required settings from the Observe install guide **plus**
two compatibility fixes for this cluster (see `_docs/learnings.md` issues 7 and 8):

- **Host port conflict**: the astronomy-demo `otel-collector-agent` DaemonSet already
  holds ports 4317/4318/6831/14250/14268/9411 on every node — `hostPort` must be `0`.
- **Insufficient CPU**: t3.large nodes allocate only ~1930m. With the demo running,
  one node has ~180m free — below the default 300m request. Fixed at 150m.

```bash
helm upgrade observe-agent observe/agent \
  --version 0.86.1 \
  --namespace observe \
  -f observe-values.yaml \
  --wait
```

### Step 13 — Verify all Observe pods are Running

```bash
kubectl get pods -n observe
```

Expected:
```
observe-agent-cluster-events-*         1/1  Running
observe-agent-cluster-metrics-*        1/1  Running
observe-agent-forwarder-agent-<a>      1/1  Running   ← one per node
observe-agent-forwarder-agent-<b>      1/1  Running   ← one per node
observe-agent-monitor-*                1/1  Running
observe-agent-node-logs-metrics-<a>    1/1  Running   ← one per node
observe-agent-node-logs-metrics-<b>    1/1  Running   ← one per node
```

If any forwarder pod is still Pending after the upgrade, describe it and check Events:
```bash
kubectl describe pod -n observe <pod-name> | tail -20
```

### Step 14 — Verify agent health

```bash
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=node-logs-metrics -o name | head -1) \
  -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'
# Expect: {"status":"Server available", ...}
```

### Step 15 — Verify data in Observe UI

Log into your Observe tenant. Within 5 minutes you should see:
- **Kubernetes app** → nodes, pods, namespaces, resource metrics
- **Logs** → container logs from all pods
- **Traces** → spans from astronomy-demo services (forwarded via `otel-demo-override.yaml`)

---

## Part 4 — Day-2 Operations

### Reconnect after AWS session expiry

```bash
aws configure          # re-enter credentials if using access keys
aws eks update-kubeconfig --region ap-southeast-2 --name astronomy-demo
```

### Update Observe agent config

Edit `observe-values.yaml`, then:
```bash
helm upgrade observe-agent observe/agent \
  --version 0.86.1 \
  --namespace observe \
  -f observe-values.yaml \
  --wait
```

### Update otel-demo config

Edit `otel-demo-override.yaml`, then:
```bash
helm upgrade my-otel-demo open-telemetry/opentelemetry-demo \
  --version 0.40.7 \
  --namespace default \
  -f otel-demo-override.yaml \
  --wait
```

### Port-forward shortcuts

```bash
kubectl port-forward svc/frontend-proxy 8080:8080 -n default &  # App UI
kubectl port-forward svc/grafana        3000:3000 -n default &  # Grafana
kubectl port-forward svc/jaeger         16686:16686 -n default & # Jaeger
```

---

## Part 5 — Full Teardown

Run strictly in this order. Removing Helm releases before Terraform destroy prevents
orphaned cloud resources from blocking VPC deletion.

> **Note:** This chart uses `emptyDir` storage only — no PersistentVolumeClaims are
> created and no EBS volumes need manual cleanup.

### Step 1 — Uninstall Helm releases

```bash
# Remove Observe agent — wait for all pods to terminate
helm uninstall observe-agent -n observe --wait

# Delete the secret manually — it is NOT deleted by helm uninstall
# (it was created with kubectl, not helm)
kubectl delete secret agent-credentials -n observe --ignore-not-found

# Remove OpenTelemetry demo app — wait for all pods to terminate
helm uninstall my-otel-demo -n default --wait

# Delete the observe namespace
kubectl delete namespace observe --wait

# Confirm all pods are gone before proceeding
kubectl get pods -n default
kubectl get pods -n observe 2>/dev/null || echo "namespace gone"
```

### Step 2 — Check for orphaned EC2 instances

EKS sometimes leaves instances running after a failed node group. These will prevent
a clean VPC destroy if they hold ENIs in the subnets.

```bash
aws ec2 describe-instances \
  --region ap-southeast-2 \
  --filters "Name=tag:eks:cluster-name,Values=astronomy-demo" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].{ID: InstanceId, IP: PrivateIpAddress}' \
  --output table
```

If any instances appear that Terraform doesn't know about, terminate them:
```bash
aws ec2 describe-instances \
  --region ap-southeast-2 \
  --filters "Name=tag:eks:cluster-name,Values=astronomy-demo" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text | xargs aws ec2 terminate-instances \
    --region ap-southeast-2 --instance-ids
```

### Step 3 — Terraform destroy

```bash
terraform destroy
```

> **Expected duration**: ~10–15 min. EKS cluster deletion is the longest step.

### Step 4 — Clean up CloudWatch log groups (optional — avoids ongoing cost)

EKS control plane logs persist in CloudWatch after the cluster is deleted.

```bash
aws logs delete-log-group \
  --log-group-name /aws/eks/astronomy-demo/cluster \
  --region ap-southeast-2
```

### Step 5 — Remove local kubeconfig entry

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
kubectl config delete-cluster \
  arn:aws:eks:ap-southeast-2:${ACCOUNT_ID}:cluster/astronomy-demo
kubectl config delete-context \
  arn:aws:eks:ap-southeast-2:${ACCOUNT_ID}:cluster/astronomy-demo
```

---

## Key values reference

| Item | Value |
|------|-------|
| AWS region | `ap-southeast-2` |
| Cluster name | `astronomy-demo` |
| Kubernetes version | `1.35` |
| Node type | `t3.large` (2 nodes) |
| Node allocatable CPU | `1930m` per node (not 2000m — kubelet reserves 70m) |
| VPC CIDR | `10.0.0.0/16` |
| Private subnets | `10.0.1.0/24`, `10.0.2.0/24` |
| Observe tenant | `177179220164` |
| Observe endpoint | `https://177179220164.collect.observeinc.com/` |
| Observe namespace | `observe` |
| Observe Helm release | `observe-agent` |
| Demo app Helm release | `my-otel-demo` |
| Observe forwarder service | `observe-agent-forwarder.observe.svc.cluster.local:4317` |
| otel-demo chart version | `0.40.7` |
| observe/agent chart version | `0.86.1` |

---

## Troubleshooting quick links

- Known issues and root causes: `_docs/learnings.md`
- Diagnostic command reference: `_docs/diagnostics.md`
