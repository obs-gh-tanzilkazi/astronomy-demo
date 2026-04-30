# EKS Diagnostic Commands

Quick reference for troubleshooting `astronomy-demo` in `ap-southeast-2`.

---

## Setup

```bash
# Configure kubectl (run this first in any new terminal session)
aws eks update-kubeconfig --region ap-southeast-2 --name astronomy-demo
```

---

## Cluster Health

```bash
# Nodes — expect all Ready
kubectl get nodes -o wide

# Core system pods — expect vpc-cni, kube-proxy, coredns all Running
kubectl get pods -n kube-system -o wide

# Recent events (errors, warnings)
kubectl get events -n kube-system --sort-by='.lastTimestamp' | tail -30

# Installed add-ons and their status
aws eks list-addons \
  --cluster-name astronomy-demo \
  --region ap-southeast-2 \
  --output table

# Describe a specific add-on
aws eks describe-addon \
  --cluster-name astronomy-demo \
  --addon-name vpc-cni \
  --region ap-southeast-2 \
  --query 'addon.{status: status, health: health}' \
  --output json

# Access entries (who can access the cluster)
aws eks list-access-entries \
  --cluster-name astronomy-demo \
  --region ap-southeast-2 \
  --output table
```

---

## Node Group

```bash
# List node groups
aws eks list-nodegroups \
  --cluster-name astronomy-demo \
  --region ap-southeast-2 \
  --output table

# Describe a node group (health, status, instance IDs)
aws eks describe-nodegroup \
  --cluster-name astronomy-demo \
  --nodegroup-name default \
  --region ap-southeast-2 \
  --query 'nodegroup.{status: status, health: health}' \
  --output json
```

---

## Node-Level Debugging

```bash
# Check kubelet logs via SSM (no SSH required)
# Step 1 — install plugin if not present: brew install --cask session-manager-plugin
# Step 2 — open shell on node
aws ssm start-session --target <instance-id> --region ap-southeast-2

# Step 3 — once inside the node
journalctl -u kubelet -n 100 --no-pager
journalctl -u containerd -n 50 --no-pager

# EC2 console/bootstrap logs (useful while instance is still starting)
aws ec2 get-console-output \
  --instance-id <instance-id> \
  --region ap-southeast-2 \
  --output text > /tmp/node-console.txt && tail -50 /tmp/node-console.txt

# List running EKS instances (find instance IDs)
aws ec2 describe-instances \
  --region ap-southeast-2 \
  --filters "Name=tag:eks:cluster-name,Values=astronomy-demo" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].{ID: InstanceId, IP: PrivateIpAddress, Launched: LaunchTime}' \
  --output table
```

---

## CloudWatch Logs

```bash
# Check log groups exist
aws logs describe-log-groups \
  --log-group-name-prefix /aws/eks/astronomy-demo \
  --region ap-southeast-2 \
  --query 'logGroups[*].logGroupName' \
  --output text

# Dump error-level control plane logs to file
aws logs filter-log-events \
  --log-group-name /aws/eks/astronomy-demo/cluster \
  --region ap-southeast-2 \
  --start-time $(python3 -c "import time; print(int((time.time() - 3600) * 1000))") \
  --filter-pattern "?Error ?Failed ?Unauthorized ?denied" \
  --query 'events[*].message' \
  --output text > /tmp/eks-errors.txt && tail -30 /tmp/eks-errors.txt

# Direct link to CloudWatch in AWS console
# https://ap-southeast-2.console.aws.amazon.com/cloudwatch/home?region=ap-southeast-2#logsV2:log-groups/log-group/%2Faws%2Feks%2Fastronomy-demo%2Fcluster
```

---

## Cleanup

```bash
# Terminate orphaned instances from failed node group attempts
aws ec2 describe-instances \
  --region ap-southeast-2 \
  --filters "Name=tag:eks:cluster-name,Values=astronomy-demo" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text | xargs aws ec2 terminate-instances \
    --region ap-southeast-2 --instance-ids
```

---

## Quick Smoke Test

```bash
# Deploy a test pod and verify DNS works inside the cluster
kubectl run test --image=busybox --restart=Never -- sleep 300
kubectl exec test -- nslookup kubernetes.default
kubectl delete pod test
```

---

## Observe Agent

```bash
# All Observe pods and their status
kubectl get pods -n observe

# Forwarder DaemonSet — expect Desired == Available (one pod per node)
kubectl get daemonset observe-agent-forwarder-agent -n observe

# Check why a pod is Pending (look at Events section at the bottom)
kubectl describe pod -n observe <pod-name>

# Confirm a pod's actual CPU/memory requests (useful after helm upgrade)
kubectl get pod -n observe <pod-name> -o jsonpath='{.spec.containers[0].resources}'

# Check injected nodeAffinity on a DaemonSet pod
kubectl get pod -n observe <pod-name> -o jsonpath='{.spec.affinity}' | python3 -m json.tool

# Find ALL pods with host port claims across the cluster (spot conflicts)
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

# Show current Helm values applied to the Observe agent release
helm get values observe-agent -n observe

# Apply updated observe-values.yaml
helm upgrade observe-agent observe/agent -n observe -f observe-values.yaml
```

---

## Observe Agent — Export Health

```bash
# Check agent health (NOTE: bound to pod IP, not localhost)
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=node-logs-metrics -o name | head -1) \
  -- sh -c 'wget -qO- ${MY_POD_IP}:13133/status'
# Expect: {"status":"Server available","upSince":"...","uptime":"..."}

# Check for export errors — filter out prometheus scrape noise
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --tail=500 \
  | grep -v "scrape" \
  | grep -iE "error|fail|500|timeout" | tail -20

kubectl logs -n observe -l app.kubernetes.io/name=cluster-metrics --tail=500 \
  | grep -v "scrape" \
  | grep -iE "error|fail|500|timeout" | tail -20

# Check if errors are current (last 5 minutes)
kubectl logs -n observe -l app.kubernetes.io/name=node-logs-metrics --since=5m \
  | grep -iE "error|fail|500|timeout"

# Verify TOKEN env var is populated inside the pod
kubectl exec -n observe \
  $(kubectl get pod -n observe -l app.kubernetes.io/name=cluster-metrics -o name | head -1) \
  -- env | grep TOKEN

# Verify secret token value (first 8 chars only — safe to log)
kubectl get secret agent-credentials -n observe \
  -o jsonpath='{.data}' | python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
[print(k, '=', base64.b64decode(v).decode()[:8]+'...') for k,v in d.items()]
"

# Test network connectivity to Observe collection endpoint
kubectl run curl-test --image=curlimages/curl --restart=Never --rm -it -- \
  curl -v -o /dev/null https://<tenant>.collect.observeinc.com/ 2>&1 | tail -10
# Expect: HTTP 401 invalid_token (means network is open, auth expected)
```

---

## OpenTelemetry Demo App (astronomy-demo)

```bash
# Check all demo app pods
kubectl get pods -n default

# Current Helm values (null = all defaults)
helm get values my-otel-demo -n default

# Apply override to add Observe as additional exporter
helm upgrade my-otel-demo open-telemetry/opentelemetry-demo -n default -f otel-demo-override.yaml

# Watch otel-collector-agent restart after upgrade
kubectl get pods -n default -l app.kubernetes.io/name=opentelemetry-collector -w

# Check otel-collector-agent logs for export errors
kubectl logs -n default -l app.kubernetes.io/name=opentelemetry-collector --tail=100 \
  | grep -iE "error|fail|refused|timeout"

# Port-forward to access the frontend UI
kubectl port-forward svc/frontend-proxy 8080:8080 -n default &
# Then open http://localhost:8080
```

---

## Node Resource Pressure

```bash
# Actual allocatable CPU per node (lower than EC2 spec due to kubelet reservation)
kubectl get nodes -o custom-columns="NAME:.metadata.name,ALLOC_CPU:.status.allocatable.cpu,ALLOC_MEM:.status.allocatable.memory"

# CPU and memory already requested/used on each node
kubectl describe nodes | grep -A 8 "Allocated resources"

# Top nodes (actual utilisation, requires metrics-server)
kubectl top nodes

# Top pods sorted by CPU
kubectl top pods --all-namespaces --sort-by=cpu | head -20
```
