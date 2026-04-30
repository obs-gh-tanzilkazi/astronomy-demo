#!/bin/bash
set -euo pipefail

# ── Auto-detect public IP ──────────────────────────────────────────────────
echo "==> Detecting public IP..."
MY_IP=$(curl -s --max-time 5 https://checkip.amazonaws.com || \
        curl -s --max-time 5 https://api.ipify.org || \
        curl -s --max-time 5 https://ifconfig.me)

if [[ -z "$MY_IP" ]]; then
  echo "ERROR: Could not detect public IP. Check internet connectivity."
  exit 1
fi

echo "    Detected: ${MY_IP}"
echo "my_ip_cidr = \"${MY_IP}/32\"" > terraform.tfvars
echo "    Written to terraform.tfvars"
echo ""

# ── Stage 1: VPC ──────────────────────────────────────────────────────────
echo "==> Stage 1: VPC"
terraform apply -target=module.vpc

echo ""
echo "==> VPC deployed. Check the AWS console to verify subnets, NAT gateway, and routing."
echo "    VPC console: https://ap-southeast-2.console.aws.amazon.com/vpc/home?region=ap-southeast-2"
echo ""
read -p "Press Enter to continue to EKS deployment, or Ctrl+C to abort..."

# ── Stage 2: EKS ──────────────────────────────────────────────────────────
echo ""
echo "==> Stage 2: EKS"
terraform apply

echo ""
echo "==> Deployment complete."
echo ""
echo "Configure kubectl:"
terraform output -raw configure_kubectl
echo ""
echo ""
echo "If EKS nodes fail to register, check CloudWatch logs:"
echo "    https://ap-southeast-2.console.aws.amazon.com/cloudwatch/home?region=ap-southeast-2#logsV2:log-groups/log-group/\$252Faws\$252Feks\$252Fastronomy-demo\$252Fcluster"
