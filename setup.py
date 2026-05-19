#!/usr/bin/env python3
"""
setup.py — Multi-project astronomy-demo setup and teardown script.

Usage:
  python setup.py --project <name> --deploy   [--step vpc|eks|otel-demo|observe]
  python setup.py --project <name> --teardown [--step vpc|eks|otel-demo|observe]

Examples:
  python setup.py --project astronomy --deploy
  python setup.py --project staging   --deploy --step observe
  python setup.py --project astronomy --teardown
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────
SCRIPT_DIR        = Path(__file__).parent.resolve()
REGION            = "ap-southeast-2"
OTEL_DEMO_CHART   = "open-telemetry/opentelemetry-demo"
OTEL_DEMO_VERSION = "0.40.7"
OBSERVE_CHART         = "observe/agent"
OBSERVE_CHART_VERSION = "0.86.1"
STEPS_ORDER = ["vpc", "eks", "otel-demo", "observe"]


# ── Config loading ─────────────────────────────────────────────────────────────
def load_config():
    """Load config.env into environment. Existing env vars take precedence."""
    env_file = SCRIPT_DIR / "config.env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key   = key.strip()
                value = value.strip().strip("'\"")  # remove surrounding quotes
                if value and key not in os.environ:
                    os.environ[key] = value
    else:
        print("WARNING: config.env not found — relying on existing environment variables.")


def require_env(key, context=""):
    val = os.environ.get(key, "")
    if not val:
        suffix = f" (needed for {context})" if context else ""
        print(f"ERROR: {key} is not set{suffix}. Fill it in config.env or export it.")
        sys.exit(1)
    return val


# ── Shell helpers ──────────────────────────────────────────────────────────────
def run(cmd, check=True, capture=False):
    """Run a shell command. Streams output unless capture=True. Exits on failure."""
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=capture,
        text=True,
        cwd=str(SCRIPT_DIR),
    )
    if check and result.returncode != 0:
        if capture and (result.stderr or result.stdout):
            print(result.stderr or result.stdout)
        sys.exit(result.returncode)
    stdout = result.stdout.strip() if result.stdout else ""
    return result.returncode, stdout


def run_ok(cmd):
    """Return True if command exits 0, False otherwise — never raises."""
    code, _ = run(cmd, check=False, capture=True)
    return code == 0


def detect_ip():
    """Detect the caller's public IP from external services."""
    for url in (
        "https://checkip.amazonaws.com",
        "https://api.ipify.org",
        "https://ifconfig.me",
    ):
        code, out = run(f"curl -s --max-time 5 {url}", check=False, capture=True)
        if code == 0 and out:
            ip = out.strip()
            print(f"  Detected public IP: {ip}")
            return ip
    print("ERROR: Could not detect public IP. Check internet connectivity.")
    sys.exit(1)


def write_tfvars(project, ip):
    tfvars = SCRIPT_DIR / "terraform.tfvars"
    tfvars.write_text(
        f'project_name = "{project}"\n'
        f'my_ip_cidr   = "{ip}/32"\n'
    )
    print(f"  terraform.tfvars written  (project={project}, my_ip_cidr={ip}/32)")


# ── Wait loops ─────────────────────────────────────────────────────────────────
def wait_for(label, check_fn, timeout=300, interval=15):
    """Poll check_fn every interval seconds until True or timeout."""
    print(f"  Waiting: {label}  (timeout {timeout}s)", flush=True)
    start = time.time()
    while True:
        if check_fn():
            print(f"  ✓ {label}")
            return True
        elapsed = int(time.time() - start)
        if elapsed >= timeout:
            print(f"  ✗ Timeout after {timeout}s waiting for: {label}")
            return False
        print(f"    [{elapsed}s] not ready, retrying in {interval}s ...", flush=True)
        time.sleep(interval)


def _nat_ready(project):
    code, out = run(
        f"aws ec2 describe-nat-gateways --region {REGION} "
        f"--filter Name=tag:Project,Values={project} Name=state,Values=available "
        f"--query 'NatGateways[0].State' --output text",
        check=False, capture=True,
    )
    return code == 0 and out.strip().lower() == "available"


def _nodes_ready():
    code, out = run("kubectl get nodes --no-headers", check=False, capture=True)
    if code != 0 or not out:
        return False
    lines = [l for l in out.splitlines() if l.strip()]
    return bool(lines) and all("Ready" in l for l in lines)


def _pods_ready(namespace):
    code, out = run(f"kubectl get pods -n {namespace} --no-headers", check=False, capture=True)
    if code != 0 or not out:
        return False
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return False
    for line in lines:
        parts = line.split()
        status = parts[2] if len(parts) > 2 else ""
        if status not in ("Running", "Completed"):
            return False
    return True


# ── Existence checks ───────────────────────────────────────────────────────────
def eks_exists(project):
    return run_ok(f"aws eks describe-cluster --name {project} --region {REGION} --output json")


def helm_release_exists(release, namespace):
    return run_ok(f"helm status {release} -n {namespace}")


def vpc_in_tf_state():
    code, out = run("terraform state list module.vpc", check=False, capture=True)
    return code == 0 and "module.vpc" in out


# ── Terraform workspace ────────────────────────────────────────────────────────
def tf_workspace_setup(project):
    _, workspaces = run("terraform workspace list", check=False, capture=True)
    if project in (workspaces or ""):
        run(f"terraform workspace select {project}")
        print(f"  Terraform workspace: selected '{project}'")
    else:
        run(f"terraform workspace new {project}")
        print(f"  Terraform workspace: created '{project}'")


# ── Deploy steps ───────────────────────────────────────────────────────────────
def step_vpc(project, state):
    print("\n── [1/4] VPC ─────────────────────────────────────────────────────────")
    if vpc_in_tf_state():
        print("  SKIP — VPC resources already in Terraform state")
        state["vpc"] = "SKIPPED"
        return

    print("  Deploying VPC ...")
    run("terraform apply -target=module.vpc -auto-approve")
    ok = wait_for(
        "NAT gateway available",
        lambda: _nat_ready(project),
        timeout=300, interval=20,
    )
    state["vpc"] = "DEPLOYED" if ok else "DEPLOYED (NAT gateway status unknown)"


def step_eks(project, state):
    print("\n── [2/4] EKS ─────────────────────────────────────────────────────────")
    if eks_exists(project):
        print(f"  SKIP — EKS cluster '{project}' already exists")
        state["eks"] = "SKIPPED"
    else:
        print("  Deploying EKS (~15–20 min) ...")
        run("terraform apply -auto-approve")
        state["eks"] = "DEPLOYED"

    print("  Configuring kubectl ...")
    run(f"aws eks update-kubeconfig --region {REGION} --name {project}")

    ok = wait_for("EKS nodes Ready", _nodes_ready, timeout=600, interval=20)
    if not ok:
        print("  WARNING: Nodes not all Ready within timeout — proceeding")
    state["nodes_ready"] = ok


def step_otel_demo(project, state):
    release   = f"{project}-otel-demo"
    namespace = project
    print(f"\n── [3/4] OTel Demo App  (release={release}, ns={namespace}) ─────────")

    if helm_release_exists(release, namespace):
        print(f"  SKIP — Helm release '{release}' already deployed")
        state["otel_demo"] = "SKIPPED"
        return

    # Build a project-specific override
    base_override = (SCRIPT_DIR / "otel-demo-override.yaml").read_text()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=f"{project}-otel-override-", delete=False
    ) as tmp:
        tmp.write(base_override)
        tmp_path = tmp.name

    try:
        print(f"  Deploying {OTEL_DEMO_CHART} {OTEL_DEMO_VERSION} ...")
        run(
            f"helm upgrade --install {release} {OTEL_DEMO_CHART} "
            f"--version {OTEL_DEMO_VERSION} "
            f"--namespace {namespace} "
            f"--create-namespace "
            f"-f {tmp_path} "
            f"--timeout 10m "
            f"--wait"
        )
        state["otel_demo"] = "DEPLOYED"
    finally:
        os.unlink(tmp_path)

    ok = wait_for(
        f"all pods Running in {namespace}",
        lambda: _pods_ready(namespace),
        timeout=600, interval=20,
    )
    if not ok:
        print("  WARNING: Some pods not Running within timeout — check: "
              f"kubectl get pods -n {namespace}")


def step_observe(project, state):
    release   = "observe-agent"
    namespace = "observe"
    print(f"\n── [4/4] Observe Agent  (release={release}, ns={namespace}) ─────────")

    observe_token    = require_env("OBSERVE_TOKEN", "Observe agent deploy")
    observe_endpoint = require_env("OBSERVE_ENDPOINT", "Observe agent deploy")

    if helm_release_exists(release, namespace):
        print(f"  SKIP — Helm release '{release}' already deployed")
        state["observe"] = "SKIPPED"
        return

    print(f"  Creating namespace '{namespace}' ...")
    run(f"kubectl create namespace {namespace}", check=False)

    print("  Creating agent-credentials secret ...")
    run(
        f"kubectl -n {namespace} create secret generic agent-credentials "
        f"--from-literal=OBSERVE_TOKEN='{observe_token}'",
        check=False,
    )
    run(
        f"kubectl annotate secret agent-credentials -n {namespace} "
        f"meta.helm.sh/release-name={release} "
        f"meta.helm.sh/release-namespace={namespace} --overwrite"
    )
    run(
        f"kubectl label secret agent-credentials -n {namespace} "
        f"app.kubernetes.io/managed-by=Helm --overwrite"
    )

    print(f"  Deploying {OBSERVE_CHART} {OBSERVE_CHART_VERSION} ...")
    run(
        f"helm upgrade --install {release} {OBSERVE_CHART} "
        f"--version {OBSERVE_CHART_VERSION} "
        f"--namespace {namespace} "
        f"--set observe.collectionEndpoint.value='{observe_endpoint}' "
        f"--set cluster.name='{project}' "
        f"--set cluster.deploymentEnvironment.name='{project}' "
        f"-f '{SCRIPT_DIR}/observe-values.yaml' "
        f"--wait"
    )
    state["observe"] = "DEPLOYED"

    ok = wait_for(
        f"all pods Running in {namespace}",
        lambda: _pods_ready(namespace),
        timeout=300, interval=15,
    )
    if not ok:
        print("  WARNING: Some Observe pods not Running within timeout — check: "
              f"kubectl get pods -n {namespace}")


# ── Teardown steps ─────────────────────────────────────────────────────────────
def teardown_observe(project):
    release   = "observe-agent"
    namespace = "observe"
    print(f"\n── Teardown: Observe agent ({release}) ───────────────────────────────")
    if helm_release_exists(release, namespace):
        run(f"helm uninstall {release} -n {namespace} --wait", check=False)
    run(f"kubectl delete secret agent-credentials -n {namespace} --ignore-not-found", check=False)
    run(f"kubectl delete namespace {namespace} --wait", check=False)
    print("  ✓ Observe agent removed")


def teardown_otel_demo(project):
    release   = f"{project}-otel-demo"
    namespace = project
    print(f"\n── Teardown: OTel Demo ({release}) ───────────────────────────────────")
    if helm_release_exists(release, namespace):
        run(f"helm uninstall {release} -n {namespace} --wait", check=False)
    run(f"kubectl delete namespace {namespace} --wait", check=False)
    print("  ✓ OTel Demo removed")


def teardown_infra(project):
    print(f"\n── Teardown: Infrastructure (workspace={project}) ────────────────────")
    run("terraform destroy -auto-approve")

    # Remove kubeconfig entry
    code, account_id = run(
        "aws sts get-caller-identity --query Account --output text",
        check=False, capture=True,
    )
    if code == 0 and account_id:
        arn = f"arn:aws:eks:{REGION}:{account_id}:cluster/{project}"
        run(f"kubectl config delete-cluster {arn}", check=False)
        run(f"kubectl config delete-context {arn}", check=False)

    # Clean up CloudWatch log group (optional, avoids ongoing cost)
    run(
        f"aws logs delete-log-group "
        f"--log-group-name /aws/eks/{project}/cluster "
        f"--region {REGION}",
        check=False,
    )

    # Switch to default workspace and delete the project workspace
    run("terraform workspace select default")
    run(f"terraform workspace delete {project}", check=False)
    print("  ✓ Infrastructure destroyed")


# ── Summary ────────────────────────────────────────────────────────────────────
def print_summary(project, state):
    observe_endpoint = os.environ.get("OBSERVE_ENDPOINT", "")
    observe_ui = observe_endpoint.replace("collect.", "").rstrip("/") if observe_endpoint else "https://<tenant>.observe.com/"
    app_ns     = project
    observe_ns = "observe"

    # Query live status
    vpc_live   = "in state" if vpc_in_tf_state() else "not found"
    eks_live, k8s_ver = _eks_status(project)
    otel_live  = _helm_status_str(f"{project}-otel-demo", app_ns) if eks_live != "not found" else "—"
    obs_live   = _helm_status_str("observe-agent", observe_ns) if eks_live != "not found" else "—"

    otel_pods = _pod_summary(app_ns) if otel_live not in ("not deployed", "—") else ""
    obs_pods  = _pod_summary(observe_ns) if obs_live not in ("not deployed", "—") else ""

    print()
    print("=" * 66)
    print(f"  COMPONENT STATUS — project: {project}")
    print("=" * 66)

    w1, w2, w3 = 18, 16, 16
    print(f"  {'Component':<{w1}} {'Status':<{w2}} {'Pods':<{w3}}")
    print(f"  {'-'*w1} {'-'*w2} {'-'*w3}")
    print(f"  {'VPC':<{w1}} {vpc_live:<{w2}} {'—':<{w3}}")
    print(f"  {'EKS':<{w1}} {eks_live:<{w2}} {('k8s ' + k8s_ver) if eks_live != 'not found' else '—':<{w3}}")
    print(f"  {'OTel Demo App':<{w1}} {otel_live:<{w2}} {otel_pods or '—':<{w3}}")
    print(f"  {'Observe Agent':<{w1}} {obs_live:<{w2}} {obs_pods or '—':<{w3}}")

    # Only show access details if the app is deployed
    if otel_live not in ("not deployed", "—"):
        print()
        print("=" * 66)
        print("  ACCESS DETAILS")
        print("=" * 66)

        access = [
            ("kubectl",    f"aws eks update-kubeconfig --region {REGION} --name {project}"),
            ("Frontend",   f"kubectl port-forward svc/frontend-proxy 8080:8080 -n {app_ns}"),
            ("Grafana",    f"kubectl port-forward svc/grafana 3000:3000 -n {app_ns}"),
            ("Jaeger",     f"kubectl port-forward svc/jaeger 16686:16686 -n {app_ns}"),
            ("Observe UI", observe_ui),
        ]
        w4, w5 = 13, 51
        print(f"  {'Resource':<{w4}} {'Command / URL':<{w5}}")
        print(f"  {'-'*w4} {'-'*w5}")
        for resource, value in access:
            print(f"  {resource:<{w4}} {value:<{w5}}")
        print("=" * 66)

    print()


# ── Status ────────────────────────────────────────────────────────────────────
def _pod_summary(namespace):
    """Return a string like '5/6 Running' or 'no pods' for a namespace."""
    code, out = run(f"kubectl get pods -n {namespace} --no-headers", check=False, capture=True)
    if code != 0 or not out:
        return "no pods"
    lines = [l for l in out.splitlines() if l.strip()]
    total   = len(lines)
    running = sum(1 for l in lines if l.split()[2] in ("Running", "Completed") if len(l.split()) > 2)
    return f"{running}/{total} Running"


def _helm_status_str(release, namespace):
    """Return Helm deployment status string, or 'not deployed'."""
    code, out = run(f"helm status {release} -n {namespace} --output json", check=False, capture=True)
    if code != 0:
        return "not deployed"
    try:
        import json
        data = json.loads(out)
        return data.get("info", {}).get("status", "unknown")
    except Exception:
        return "deployed"


def _eks_status(project):
    """Return (status_str, k8s_version) for the EKS cluster."""
    code, out = run(
        f"aws eks describe-cluster --name {project} --region {REGION} --output json",
        check=False, capture=True,
    )
    if code != 0:
        return "not found", "—"
    try:
        import json
        data    = json.loads(out)
        cluster = data.get("cluster", {})
        status  = cluster.get("status", "unknown").lower()
        version = cluster.get("version", "—")
        return status, version
    except Exception:
        return "unknown", "—"


def show_status(project):
    app_ns      = project
    observe_ns  = "observe"
    otel_release   = f"{project}-otel-demo"
    observe_release = "observe-agent"

    print(f"\n{'='*66}")
    print(f"  STATUS — project: {project}")
    print(f"{'='*66}\n")

    # ── Infrastructure ─────────────────────────────────────────────────────────
    vpc_status = "in state" if vpc_in_tf_state() else "not in state"
    eks_status, k8s_ver = _eks_status(project)

    print(f"  {'Component':<22} {'Status':<16} {'Detail'}")
    print(f"  {'-'*22} {'-'*16} {'-'*22}")
    print(f"  {'VPC (Terraform)':<22} {vpc_status:<16} workspace: {project}")
    print(f"  {'EKS':<22} {eks_status:<16} k8s {k8s_ver}")

    # ── Kubernetes layer (only if EKS exists) ──────────────────────────────────
    if eks_exists(project):
        run(f"aws eks update-kubeconfig --region {REGION} --name {project}", check=False)

        otel_helm   = _helm_status_str(otel_release, app_ns)
        otel_pods   = _pod_summary(app_ns) if otel_helm != "not deployed" else "—"
        obs_helm    = _helm_status_str(observe_release, observe_ns)
        obs_pods    = _pod_summary(observe_ns) if obs_helm != "not deployed" else "—"

        print(f"  {'OTel Demo App':<22} {otel_helm:<16} {otel_pods}  (ns: {app_ns})")
        print(f"  {'Observe Agent':<22} {obs_helm:<16} {obs_pods}  (ns: {observe_ns})")

        # ── Node summary ───────────────────────────────────────────────────────
        code, out = run("kubectl get nodes --no-headers", check=False, capture=True)
        if code == 0 and out:
            lines = [l for l in out.splitlines() if l.strip()]
            ready = sum(1 for l in lines if "Ready" in l)
            print(f"\n  Nodes: {ready}/{len(lines)} Ready")

            print(f"\n  {'NODE':<46} {'STATUS':<10} {'VERSION'}")
            print(f"  {'-'*46} {'-'*10} {'-'*14}")
            for line in lines:
                parts  = line.split()
                name   = parts[0] if len(parts) > 0 else "?"
                status = parts[1] if len(parts) > 1 else "?"
                ver    = parts[4] if len(parts) > 4 else "?"
                print(f"  {name:<46} {status:<10} {ver}")
    else:
        print(f"  {'OTel Demo App':<22} {'—':<16} (EKS not running)")
        print(f"  {'Observe Agent':<22} {'—':<16} (EKS not running)")

    print()


# ── Preflight checks ──────────────────────────────────────────────────────────
REQUIRED_TOOLS = {
    "terraform": "brew tap hashicorp/tap && brew install hashicorp/tap/terraform",
    "aws":       "brew install awscli",
    "kubectl":   "brew install kubectl",
    "helm":      "brew install helm",
    "curl":      "brew install curl",
}

HELM_REPOS = {
    "observe":        "https://observeinc.github.io/helm-charts",
    "open-telemetry": "https://open-telemetry.github.io/opentelemetry-helm-charts",
}


def check_prerequisites():
    """Verify all required CLI tools are on PATH."""
    import shutil
    missing = []
    for tool, install_hint in REQUIRED_TOOLS.items():
        if not shutil.which(tool):
            missing.append((tool, install_hint))
    if missing:
        print("ERROR: Missing required tools:\n")
        for tool, hint in missing:
            print(f"  • {tool:12s} → install with: {hint}")
        print()
        sys.exit(1)


def ensure_helm_repos():
    """Ensure required Helm chart repositories are added."""
    for name, url in HELM_REPOS.items():
        run(f"helm repo add --force-update {name} {url}", check=False, capture=True)
    run("helm repo update", check=False, capture=True)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Deploy or tear down the astronomy demo stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", required=True,
        help="Project name — used for all resource names and tags",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deploy",   action="store_true", help="Deploy the stack")
    mode.add_argument("--teardown", action="store_true", help="Tear down the stack")
    mode.add_argument("--status",   action="store_true", help="Show current deployment status")
    parser.add_argument(
        "--step", choices=STEPS_ORDER,
        help="Run one specific step only",
    )
    args = parser.parse_args()

    load_config()
    check_prerequisites()
    ensure_helm_repos()

    project = args.project
    print(f"\n{'='*66}")
    if args.status:
        mode_label = "status"
    elif args.deploy:
        mode_label = "deploy"
    else:
        mode_label = "teardown"
    print(f"  Project : {project}")
    print(f"  Mode    : {mode_label}")
    if args.step:
        print(f"  Step    : {args.step}")
    print(f"{'='*66}\n")

    # Ensure terraform is initialised
    if not (SCRIPT_DIR / ".terraform").exists():
        print("==> terraform init")
        run("terraform init")

    tf_workspace_setup(project)

    # ── Status ─────────────────────────────────────────────────────────────────
    if args.status:
        show_status(project)
        return

    # ── Deploy ─────────────────────────────────────────────────────────────────
    if args.deploy:
        ip = detect_ip()
        write_tfvars(project, ip)

        state = {}
        steps_to_run = [args.step] if args.step else STEPS_ORDER
        for step in steps_to_run:
            if step == "vpc":
                step_vpc(project, state)
            elif step == "eks":
                step_eks(project, state)
            elif step == "otel-demo":
                step_otel_demo(project, state)
            elif step == "observe":
                step_observe(project, state)

        print_summary(project, state)

    # ── Teardown ───────────────────────────────────────────────────────────────
    elif args.teardown:
        # Configure kubectl so helm/kubectl commands work
        if eks_exists(project):
            run(f"aws eks update-kubeconfig --region {REGION} --name {project}", check=False)

        # Teardown in reverse order; infra covers both vpc + eks
        if args.step:
            steps_to_run = [args.step]
        else:
            steps_to_run = list(reversed(STEPS_ORDER))

        infra_torn_down = False
        for step in steps_to_run:
            if step == "observe":
                teardown_observe(project)
            elif step == "otel-demo":
                teardown_otel_demo(project)
            elif step in ("eks", "vpc") and not infra_torn_down:
                teardown_infra(project)
                infra_torn_down = True

        print(f"\n✓ Teardown complete for project '{project}'\n")


if __name__ == "__main__":
    main()
