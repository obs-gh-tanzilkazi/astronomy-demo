# Astronomy Demo — Runbook

Full setup and teardown of an EKS cluster, OpenTelemetry demo app,
and Observe monitoring agent in AWS `ap-southeast-2`.

---

## Part 1 — Prerequisites

### Tools required

```bash
terraform version        # >= 1.5.0
aws --version            # >= 2.x
kubectl version --client
helm version             # >= 3.x
python3 --version        # >= 3.8
```

Install missing tools:
```bash
brew tap hashicorp/tap && brew install hashicorp/tap/terraform
brew install awscli kubectl helm python3
```

### AWS access

```bash
aws configure
# Enter: Access Key ID, Secret Access Key, region: ap-southeast-2, output: json

# Verify
aws sts get-caller-identity
```

### Helm chart repositories

```bash
helm repo add observe https://observeinc.github.io/helm-charts
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
```

### config.env (REQUIRED)

This file **must** be created before running `setup.py`:

- Create `config.env` in the project root with the values below
- AWS keys can be left blank — if empty, the script picks up credentials from environment variables or `~/.aws/credentials`

```
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=ap-southeast-2
OBSERVE_TOKEN=<customerid>:<token>
OBSERVE_ENDPOINT=https://<your-customer-id>.collect.observeinc.com/
```

> Do NOT wrap values in quotes.

---

## Part 2 — Project Structure

```
astronomy-demo/
├── setup.py                   # Main orchestration script
├── config.env                 # Secrets (gitignored)
├── main.tf                    # VPC + EKS infrastructure
├── variables.tf               # Input variables (project_name, my_ip_cidr)
├── observe-values.yaml        # Observe agent Helm overrides
├── otel-demo-override.yaml    # OTel demo Helm overrides (adds Observe exporter)
└── _docs/
    ├── runbook.md             # This file
    ├── learnings.md           # Troubleshooting history
    └── diagnostics.md         # Manual deploy commands & diagnostics
```

---

## Part 3 — Deploy

### Full deploy

```bash
python3 setup.py --project <name> --deploy
```

This runs four steps sequentially:

1. **VPC** — creates VPC, subnets, NAT gateway, route tables
2. **EKS** — creates the EKS cluster and 2x t3.large managed nodes
3. **OTel Demo** — deploys the OpenTelemetry astronomy demo app (~20 microservices)
4. **Observe Agent** — creates namespace, secret, installs the Observe agent chart

Each step checks if the resource already exists and skips if so.

### Deploy a single step

```bash
python3 setup.py --project <name> --deploy --step vpc
python3 setup.py --project <name> --deploy --step eks
python3 setup.py --project <name> --deploy --step otel-demo
python3 setup.py --project <name> --deploy --step observe
```

### Check status

```bash
python3 setup.py --project <name> --status
```

Shows live status of all components, pod health, and node state.

---

## Part 4 — Day-2 Operations

### Reconnect after AWS session expiry

```bash
aws configure
aws eks update-kubeconfig --region ap-southeast-2 --name <project>
```

### Access the frontend

```bash
kubectl port-forward svc/frontend-proxy 8080:8080 -n <project>
open http://localhost:8080
```

### Update Observe agent config

Edit `observe-values.yaml`, then redeploy the observe step:

```bash
python3 setup.py --project <name> --deploy --step observe
```

### Update OTel demo config

Edit `otel-demo-override.yaml`, then redeploy:

```bash
python3 setup.py --project <name> --deploy --step otel-demo
```

---

## Part 5 — Teardown

### Full teardown

```bash
python3 setup.py --project <name> --teardown
```

This runs in reverse order:
1. Uninstalls Observe agent + deletes secret + deletes `observe` namespace
2. Uninstalls OTel demo + deletes project namespace
3. Runs `terraform destroy` (removes EKS + VPC)
4. Cleans up CloudWatch log group and kubeconfig entries
5. Deletes the Terraform workspace

### Teardown a single step

```bash
python3 setup.py --project <name> --teardown --step observe
python3 setup.py --project <name> --teardown --step otel-demo
python3 setup.py --project <name> --teardown --step eks   # destroys all infra
```

---

## Part 6 — Common Troubleshooting Commands

### Cluster health

| Command | When to use |
|---------|-------------|
| `kubectl get nodes -o wide` | Check if nodes are Ready and see instance IPs |
| `kubectl get pods -A` | Overview of all pods across all namespaces |
| `kubectl get pods -n kube-system` | Verify core components (vpc-cni, coredns, kube-proxy) |
| `kubectl top nodes` | Check CPU/memory pressure on nodes |
| `kubectl top pods -n <ns>` | Find resource-hungry pods |

### Pod issues

| Command | When to use |
|---------|-------------|
| `kubectl describe pod <pod> -n <ns>` | Pod stuck Pending or CrashLooping — shows Events |
| `kubectl logs <pod> -n <ns> --tail=50` | Read recent container logs |
| `kubectl logs <pod> -n <ns> --previous` | Read logs from the last crash (OOMKilled, etc.) |
| `kubectl get events -n <ns> --sort-by=.lastTimestamp` | Recent events sorted by time |
| `kubectl delete pod <pod> -n <ns>` | Force restart a pod (controller recreates it) |

### Observe agent

| Command | When to use |
|---------|-------------|
| `kubectl get pods -n observe` | Check all Observe pods are Running |
| `kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --tail=50` | Check for export errors (401, 500, timeout) |
| `kubectl logs -n observe -l app.kubernetes.io/name=forwarder --tail=50` | Check forwarder receiving app telemetry |
| `kubectl get secret agent-credentials -n observe -o jsonpath='{.data.OBSERVE_TOKEN}' \| base64 -d` | Verify the token stored in the secret |
| `kubectl exec -n observe <pod> -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'` | Health check (expect "Server available") |
| `kubectl rollout restart daemonset -n observe` | Restart all DaemonSet pods after config change |
| `kubectl rollout restart deployment -n observe` | Restart all Deployment pods after config change |

### OTel demo app

| Command | When to use |
|---------|-------------|
| `kubectl get pods -n <project>` | Check demo app pods status |
| `kubectl get configmap <project>-otel-demo-otelcol -n <project> -o jsonpath='{.data.relay}'` | View the OTel collector config |
| `kubectl logs -n <project> -l app.kubernetes.io/component=otel-collector --tail=50` | Check collector for export errors |
| `helm get values <project>-otel-demo -n <project>` | View effective Helm values |

### Helm

| Command | When to use |
|---------|-------------|
| `helm list -A` | Show all installed releases |
| `helm status <release> -n <ns>` | Check release status and last deploy time |
| `helm history <release> -n <ns>` | View revision history (for rollback decisions) |
| `helm rollback <release> <revision> -n <ns>` | Roll back to a previous revision |

### Terraform / AWS

| Command | When to use |
|---------|-------------|
| `terraform workspace list` | See all project workspaces |
| `terraform state list` | Check what resources Terraform knows about |
| `aws eks describe-cluster --name <project> --region ap-southeast-2` | Verify EKS cluster exists and its status |
| `aws ec2 describe-instances --region ap-southeast-2 --filters "Name=tag:Project,Values=<project>"` | Find EC2 instances for a project |

### Network / connectivity

| Command | When to use |
|---------|-------------|
| `kubectl run curl-test --image=curlimages/curl --rm -it --restart=Never -- curl -s <url>` | Test connectivity from inside the cluster |
| `kubectl get svc -n <ns>` | View service ClusterIPs and ports |
| `kubectl get svc -A --field-selector spec.type=NodePort` | Find externally exposed services |

---

## Troubleshooting resources

- Known issues and root causes: `_docs/learnings.md`
- Diagnostic command reference: `_docs/diagnostics.md`
