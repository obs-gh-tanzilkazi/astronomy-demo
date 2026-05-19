# Manual Deployment & Diagnostics

Manual commands for deploying and tearing down the demo environment without `setup.py`.
Also includes diagnostic commands and gotchas discovered during operation.

---

## Manual Deployment (without setup.py)

### 1. Detect public IP and write terraform.tfvars

```bash
MY_IP=$(curl -s https://checkip.amazonaws.com)
cat > terraform.tfvars <<EOF
project_name = "<your-project-name>"
my_ip_cidr   = "${MY_IP}/32"
EOF
```

### 2. Initialise Terraform and select workspace

```bash
terraform init
terraform workspace new <project>   # or: terraform workspace select <project>
```

### 3. Deploy VPC

```bash
terraform apply -target=module.vpc -auto-approve
```

### 4. Deploy EKS

```bash
terraform apply -auto-approve
```

> Expected duration: ~15–20 minutes.

### 5. Configure kubectl

```bash
aws eks update-kubeconfig --region ap-southeast-2 --name <project>
```

### 6. Verify cluster health

```bash
kubectl get nodes -o wide            # Expect 2x Ready
kubectl get pods -n kube-system      # Expect vpc-cni, kube-proxy, coredns Running
```

### 7. Deploy OTel demo app

```bash
helm upgrade --install <project>-otel-demo open-telemetry/opentelemetry-demo \
  --version 0.40.7 \
  --namespace <project> \
  --create-namespace \
  -f otel-demo-override.yaml \
  --timeout 10m \
  --wait
```

### 8. Deploy Observe agent

```bash
# Create namespace
kubectl create namespace observe

# Create secret
kubectl -n observe create secret generic agent-credentials \
  --from-literal=OBSERVE_TOKEN='<your-token>'

# Annotate and label for Helm ownership
kubectl annotate secret agent-credentials -n observe \
  meta.helm.sh/release-name=observe-agent \
  meta.helm.sh/release-namespace=observe

kubectl label secret agent-credentials -n observe \
  app.kubernetes.io/managed-by=Helm

# Install the chart
helm upgrade --install observe-agent observe/agent \
  --version 0.86.1 \
  --namespace observe \
  --set observe.collectionEndpoint.value='<endpoint>' \
  --set cluster.name='<project>' \
  --set cluster.deploymentEnvironment.name='<project>' \
  -f observe-values.yaml \
  --wait
```

---

## Manual Teardown (without setup.py)

Run in this exact order:

```bash
# 1. Remove Observe agent
helm uninstall observe-agent -n observe --wait
kubectl delete secret agent-credentials -n observe --ignore-not-found
kubectl delete namespace observe --wait

# 2. Remove OTel demo
helm uninstall <project>-otel-demo -n <project> --wait
kubectl delete namespace <project> --wait

# 3. Destroy infrastructure
terraform destroy -auto-approve

# 4. Clean up CloudWatch logs (avoids ongoing cost)
aws logs delete-log-group \
  --log-group-name /aws/eks/<project>/cluster \
  --region ap-southeast-2

# 5. Remove kubeconfig entry
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
kubectl config delete-cluster arn:aws:eks:ap-southeast-2:${ACCOUNT_ID}:cluster/<project>
kubectl config delete-context arn:aws:eks:ap-southeast-2:${ACCOUNT_ID}:cluster/<project>

# 6. Delete workspace
terraform workspace select default
terraform workspace delete <project>
```

---

## Gotchas

### Observe chart MUST use namespace `observe`

The chart internally hardcodes ClusterRole/ClusterRoleBinding references to the `observe`
namespace. Using a custom namespace (e.g. `<project>-observe`) causes:
```
namespaces "observe" not found
```
**Always use the fixed `observe` namespace.** Isolation is handled by separate EKS clusters.

### Stale ClusterRoles block re-install after failed Helm release

If a `helm install` fails partway through, cluster-scoped resources (ClusterRole,
ClusterRoleBinding) survive namespace deletion. A subsequent install with a different
release name fails with:
```
annotation "meta.helm.sh/release-name" must equal "X": current value is "Y"
```
**Fix:** Find and delete the stale resources:
```bash
kubectl get clusterrole,clusterrolebinding -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('items', []):
    ann = item.get('metadata', {}).get('annotations', {})
    if ann.get('meta.helm.sh/release-name') == '<old-release-name>':
        print(f\"{item['kind']}/{item['metadata']['name']}\")
"
# Then delete each one
```

### Quoted token in secret causes 401

If `config.env` has `OBSERVE_TOKEN="value"` (with quotes), the literal quote characters
end up in the Kubernetes secret. Observe rejects the token with HTTP 401.
**Fix:** Never wrap values in quotes in `config.env`. Verify with:
```bash
kubectl get secret agent-credentials -n observe -o jsonpath='{.data.OBSERVE_TOKEN}' | base64 -d
# Should NOT have " at start/end
```

### Host port conflict — forwarder pods stuck Pending

The OTel demo deploys an `otel-collector-agent` DaemonSet that binds host ports
4317, 4318, 6831, 14250, 14268, 9411. The Observe forwarder tries the same ports.
**Fix:** `observe-values.yaml` sets `hostPort: 0` for all forwarder ports. The forwarder
is still reachable via its ClusterIP service.

### Insufficient CPU — forwarder pods stuck Pending

t3.large nodes have only ~1930m allocatable CPU (not 2000m — kubelet reserves 70m).
With the demo app running, one node may have only ~180m free. The forwarder's default
300m request doesn't fit.
**Fix:** `observe-values.yaml` reduces the CPU request to 150m.

### Terraform destroy fails after main.tf changes

If you modify resource names in `main.tf` (e.g. adding `var.project_name`) after deploying,
`terraform destroy` will try to rename resources instead of deleting them.
**Fix:** Either revert `main.tf` to match the deployed state, then destroy; or pass the
old project name via `-var`:
```bash
terraform destroy -var='project_name=<old-name>' -var='my_ip_cidr=0.0.0.0/32'
```

### Accounting pod OOMKilled

The accounting service (.NET) exceeds its default 120Mi memory limit on startup.
**Fix:** `otel-demo-override.yaml` bumps it to 300Mi.

### Agent health endpoint is NOT on localhost

The OTel health extension binds to `${MY_POD_IP}`, not `0.0.0.0` or `127.0.0.1`.
```bash
# Wrong — will get connection refused:
kubectl exec <pod> -- wget -qO- localhost:13133/status

# Correct:
kubectl exec <pod> -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'
```

---

## Diagnostic Commands

### Cluster health

```bash
kubectl get nodes -o wide
kubectl get pods -n kube-system -o wide
kubectl get events -n kube-system --sort-by='.lastTimestamp' | tail -30

aws eks list-addons --cluster-name <project> --region ap-southeast-2 --output table
aws eks describe-addon --cluster-name <project> --addon-name vpc-cni --region ap-southeast-2 \
  --query 'addon.{status: status, health: health}' --output json
```

### Node group

```bash
aws eks list-nodegroups --cluster-name <project> --region ap-southeast-2 --output table
aws eks describe-nodegroup --cluster-name <project> --nodegroup-name default \
  --region ap-southeast-2 --query 'nodegroup.{status: status, health: health}' --output json
```

### Node resource pressure

```bash
kubectl get nodes -o custom-columns="NAME:.metadata.name,ALLOC_CPU:.status.allocatable.cpu,ALLOC_MEM:.status.allocatable.memory"
kubectl describe nodes | grep -A 8 "Allocated resources"
kubectl top nodes
kubectl top pods --all-namespaces --sort-by=cpu | head -20
```

### Node-level debugging (SSM)

```bash
# Open shell on node (no SSH key required)
aws ssm start-session --target <instance-id> --region ap-southeast-2

# Once inside:
journalctl -u kubelet -n 100 --no-pager
journalctl -u containerd -n 50 --no-pager
```

### Find EC2 instances

```bash
aws ec2 describe-instances --region ap-southeast-2 \
  --filters "Name=tag:Project,Values=<project>" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].{ID: InstanceId, IP: PrivateIpAddress, Launched: LaunchTime}' \
  --output table
```

### Observe agent

```bash
kubectl get pods -n observe
kubectl get daemonset observe-agent-forwarder-agent -n observe
kubectl describe pod -n observe <pod-name>
kubectl get pod -n observe <pod-name> -o jsonpath='{.spec.containers[0].resources}'

# Health check
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=node-logs-metrics -o name | head -1) \
  -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'

# Export errors
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --tail=100 \
  | grep -iE "error|fail|401|500|timeout"

# Verify token
kubectl get secret agent-credentials -n observe -o jsonpath='{.data.OBSERVE_TOKEN}' | base64 -d

# Restart after config change
kubectl rollout restart daemonset -n observe
kubectl rollout restart deployment -n observe
```

### OTel demo app

```bash
kubectl get pods -n <project>
helm get values <project>-otel-demo -n <project>

# View rendered collector config
kubectl get configmap <project>-otel-demo-otelcol -n <project> -o jsonpath='{.data.relay}'

# Collector export errors
kubectl logs -n <project> -l app.kubernetes.io/name=opentelemetry-collector --tail=100 \
  | grep -iE "error|fail|refused|timeout"
```

### Host port conflicts

```bash
# Find ALL pods claiming host ports across the cluster
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
```

### CloudWatch logs

```bash
aws logs describe-log-groups \
  --log-group-name-prefix /aws/eks/<project> \
  --region ap-southeast-2 \
  --query 'logGroups[*].logGroupName' --output text

aws logs filter-log-events \
  --log-group-name /aws/eks/<project>/cluster \
  --region ap-southeast-2 \
  --start-time $(python3 -c "import time; print(int((time.time() - 3600) * 1000))") \
  --filter-pattern "?Error ?Failed ?Unauthorized ?denied" \
  --query 'events[*].message' --output text | tail -30
```

### Network connectivity test

```bash
kubectl run curl-test --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s -o /dev/null -w "%{http_code}" http://observe-agent-forwarder.observe.svc.cluster.local:4317
```

### Cleanup orphaned instances

```bash
aws ec2 describe-instances --region ap-southeast-2 \
  --filters "Name=tag:Project,Values=<project>" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' --output text \
  | xargs aws ec2 terminate-instances --region ap-southeast-2 --instance-ids
```
