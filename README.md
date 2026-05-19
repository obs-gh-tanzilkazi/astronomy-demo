# Astronomy Demo

Automated deployment of an EKS cluster, OpenTelemetry demo app, and Observe
monitoring agent in AWS `ap-southeast-2`.

---

## Prerequisites

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

## Design Decisions

- **Multi-project support** via `--project` + Terraform workspaces — you can run multiple isolated instances side by side
- **Idempotent** — each step checks if resources already exist before deploying (skips VPC if in state, skips EKS if cluster exists, skips Helm releases if already deployed)
- **Wait loops with timeouts** for NAT gateway, node readiness, and pod readiness
- **Dynamic namespacing** — each project gets its own namespace; multiple demo environments can run simultaneously on the same cluster
- **Three modes**: `--deploy`, `--teardown`, `--status`
- **Granular step control** via `--step` for rerunning individual stages
- **Clean teardown** — reverse-order removal including CloudWatch log group cleanup and kubeconfig entry removal

---

## Project Structure

```
astronomy-demo/
├── setup.py                   # Main orchestration script
├── config.env                 # Secrets (gitignored)
├── main.tf                    # VPC + EKS infrastructure
├── variables.tf               # Input variables (project_name, my_ip_cidr)
├── observe-values.yaml        # Observe agent Helm overrides
├── otel-demo-override.yaml    # OTel demo Helm overrides (adds Observe exporter)
└── _docs/
    ├── runbook.md             # Operational runbook with troubleshooting
    ├── learnings.md           # Troubleshooting history
    └── diagnostics.md         # Manual deploy commands & diagnostics
```

---

## Deploy

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

## Day-2 Operations

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

## Teardown

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

## Troubleshooting

See `_docs/runbook.md` for detailed troubleshooting commands and `_docs/learnings.md`
for a history of issues encountered and their fixes.
