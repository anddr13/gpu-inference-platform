#!/usr/bin/env bash
# Read-only checks for the EKS lab. Creates nothing, changes nothing.
#
#   preflight.sh            before `make eks-up`: identity, GPU types, quotas, Spot price, EKS version
#   preflight.sh leftovers  after `make eks-down`: things that would keep billing
#
# Env: AWS_PROFILE (gpu-lab), REGION (eu-north-1), K8S_VERSION (1.36),
#      GPU_TYPES ("g6.2xlarge g5.2xlarge"), YES=1 skips the prompt.
set -euo pipefail
cd "$(dirname "$0")"

# Same profile as the Makefile: never fall back to the default credentials.
export AWS_PROFILE="${AWS_PROFILE:-gpu-lab}"
REGION="${REGION:-eu-north-1}"
K8S_VERSION="${K8S_VERSION:-1.36}"
read -r -a TYPES <<< "${GPU_TYPES:-g6.2xlarge g5.2xlarge}"
# One g6.2xlarge / g5.2xlarge = 8 vCPU; a second GPU node needs 16.
NEED_VCPU=8
failed=0

ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; failed=1; }
aws()  { command aws --region "$REGION" "$@"; }

command -v aws >/dev/null || { echo "aws cli v2 not found: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"; exit 1; }

# none <count> <what>: OK when nothing is left, WARN with the count otherwise.
none() {
  if (( $1 == 0 )); then ok "no $2"; else warn "$1 $2 still billed"; fi
}

leftovers() {
  echo "== Billable leftovers in $REGION"
  none "$(aws eks list-clusters --query 'length(clusters)' --output text)" "EKS cluster(s)"
  none "$(aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running \
            Name=tag:Project,Values=gpu-inference-platform --query 'length(Reservations[].Instances[])' --output text)" \
       "lab EC2 instance(s)"
  none "$(aws ec2 describe-nat-gateways --filter Name=state,Values=available --query 'length(NatGateways)' --output text)" \
       "NAT gateway(s)"
  none "$(aws ec2 describe-volumes --filters Name=status,Values=available --query 'length(Volumes)' --output text)" \
       "unattached EBS volume(s)"
  none "$(aws ec2 describe-addresses --query 'length(Addresses[?AssociationId==null])' --output text)" \
       "unattached Elastic IP(s)"
  none "$(aws elbv2 describe-load-balancers --query 'length(LoadBalancers)' --output text)" "load balancer(s)"
}

if [[ "${1:-}" == "leftovers" ]]; then
  leftovers
  exit 0
fi

echo "== Identity"
arn=$(aws sts get-caller-identity --query Arn --output text) \
  || { echo "  no credentials for profile $AWS_PROFILE: aws login --remote --profile $AWS_PROFILE --region $REGION"; exit 1; }
echo "  profile $AWS_PROFILE: account $(aws sts get-caller-identity --query Account --output text) as $arn"
[[ "$arn" == *":root" ]] && warn "root user credentials: use an IAM Identity Center or IAM admin user instead"
if [[ "${YES:-}" != "1" ]]; then
  read -r -p "  Is this your own lab account, not an employer's? [y/N] " answer
  [[ "$answer" == "y" ]] || exit 1
fi

echo "== AWS account plan"
# The Free plan only launches free-tier instance types (t3.micro...). A dry-run launch of g6.2xlarge
# still says "allowed": the restriction bites when the Auto Scaling group launches, ten minutes into
# an EKS apply. The Free Tier API tells it up front.
plan=$(command aws freetier get-account-plan-state --region us-east-1 \
         --query '[accountPlanType, accountPlanRemainingCredits.amount]' --output text 2>/dev/null || true)
case "$plan" in
  FREE*)
    bad "Free plan (credits: \$${plan##*[[:space:]]}): EC2 launches only free-tier types, so no GPU or t3.large nodes."
    echo "        Upgrade in Billing → Free plan → Upgrade plan; remaining credits are applied first." ;;
  PAID*) ok "paid plan" ;;
  *)     warn "plan type unknown (Free Tier API unavailable)" ;;
esac

echo "== GPU instance types in $REGION"
for t in "${TYPES[@]}"; do
  azs=$(aws ec2 describe-instance-type-offerings --location-type availability-zone \
          --filters "Name=instance-type,Values=$t" --query 'InstanceTypeOfferings[].Location' --output text)
  if [[ -n "$azs" ]]; then ok "$t offered in: $azs"; else warn "$t not offered in $REGION"; fi
done

# SPOT unless terraform.tfvars says otherwise (same default as variables.tf).
capacity=$(sed -nE 's/^[[:space:]]*gpu_capacity_type[[:space:]]*=[[:space:]]*"([A-Z_]+)".*/\1/p' terraform.tfvars 2>/dev/null)
capacity=${capacity:-SPOT}

echo "== Can this account launch the GPU node? (dry-run, nothing starts)"
ami=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
        --query Parameter.Value --output text)
# dry_run <args...>: prints DryRunOperation (allowed) or the error code that blocks it.
# A dry run always exits non-zero, even when allowed, so its status is ignored on purpose.
dry_run() {
  { aws ec2 run-instances --dry-run --image-id "$ami" --instance-type "${TYPES[0]}" --count 1 "$@" 2>&1 || true; } \
    | { grep -oE 'DryRunOperation|UnauthorizedOperation|service control policy' || true; } | sort -u | tr '\n' ' '
}
spot_access=$(dry_run --instance-market-options MarketType=spot)
if [[ "$spot_access" == *DryRunOperation* ]]; then
  ok "Spot launches allowed"
elif [[ "$capacity" == "SPOT" ]]; then
  bad "Spot launches denied ($spot_access). AWS Free plan accounts block Spot: set gpu_capacity_type = \"ON_DEMAND\""
else
  ok "Spot denied ($spot_access), not needed: gpu_capacity_type = $capacity"
fi
if [[ "$(dry_run)" == *DryRunOperation* ]]; then
  ok "on-demand ${TYPES[0]} launches allowed"
else
  bad "on-demand ${TYPES[0]} launches denied"
fi

echo "== vCPU quota for G instances, $capacity (need >= $NEED_VCPU)"
# Prints the applied quota as an integer; 0 when unknown.
quota() {
  local value
  value=$(aws service-quotas get-service-quota --service-code ec2 --quota-code "$1" \
            --query 'Quota.Value' --output text 2>/dev/null || true)
  value=${value%.*}
  [[ "$value" =~ ^[0-9]+$ ]] || value=0
  echo "$value"
}
if [[ "$capacity" == "SPOT" ]]; then
  code=L-3819A6DF; quota_name="All G and VT Spot Instance Requests"
else
  code=L-DB2E81BA; quota_name="Running On-Demand G and VT instances"
fi
vcpu=$(quota "$code")
if (( vcpu >= NEED_VCPU )); then
  ok "$quota_name ($code): $vcpu vCPU"
else
  bad "$quota_name ($code): $vcpu vCPU, the default is 0. Request $NEED_VCPU:"
  echo "        aws service-quotas request-service-quota-increase --region $REGION --service-code ec2 --quota-code $code --desired-value $NEED_VCPU"
  # shellcheck disable=SC2016  # backticks are JMESPath literals, not shell
  pending=$(aws service-quotas list-requested-service-quota-change-history-by-quota --service-code ec2 --quota-code "$code" \
              --query 'RequestedQuotas[?Status==`PENDING` || Status==`CASE_OPENED`].DesiredValue' --output text 2>/dev/null || true)
  if [[ -n "$pending" ]]; then echo "        request already open for $pending vCPU, waiting for AWS"; fi
fi

if [[ "$capacity" == "SPOT" ]]; then
  echo "== Spot price right now (Linux, cheapest zones first)"
  aws ec2 describe-spot-price-history --instance-types "${TYPES[@]}" --product-descriptions "Linux/UNIX" \
    --start-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'SpotPriceHistory[].[InstanceType,AvailabilityZone,SpotPrice]' --output text \
    | sort -k3 -n | head -6 | awk '{printf "  %-12s %-14s $%.3f/h\n", $1, $2, $3}'
fi

echo "== EKS $K8S_VERSION in $REGION"
addons=$(aws eks describe-addon-versions --kubernetes-version "$K8S_VERSION" --addon-name vpc-cni \
           --query 'length(addons)' --output text 2>/dev/null || echo 0)
if (( addons > 0 )); then
  ok "add-ons published for $K8S_VERSION"
else
  bad "no add-ons for $K8S_VERSION: change kubernetes_version"
fi

echo "== Remote state"
if [[ -f backend.hcl ]]; then
  bucket=$(sed -nE 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' backend.hcl)
  if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    ok "state bucket $bucket"
  else
    bad "backend.hcl names bucket $bucket, which this account cannot reach: make state-up"
  fi
else
  bad "no backend.hcl: run make state-up once (S3 bucket for Terraform state)"
fi

echo "== Guard rails in terraform.tfvars"
if [[ -f terraform.tfvars ]]; then
  if grep -qE '^[[:space:]]*budget_alert_email[[:space:]]*=.*example\.com' terraform.tfvars; then
    bad "budget_alert_email is still the example address"
  elif grep -qE '^[[:space:]]*budget_alert_email[[:space:]]*=[[:space:]]*"[^"]+@' terraform.tfvars; then
    ok "budget_alert_email set"
  else
    warn "budget_alert_email not set: no cost alert"
  fi
  my_ip=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
  if grep -qE '^[[:space:]]*api_allowed_cidrs.*203\.0\.113\.' terraform.tfvars; then
    bad "api_allowed_cidrs is the example (203.0.113.x): kubectl would be locked out. Your IP: ${my_ip:-unknown}"
  elif grep -qE '^[[:space:]]*api_allowed_cidrs' terraform.tfvars; then
    if [[ -n "$my_ip" ]] && ! grep -qF "$my_ip" terraform.tfvars; then
      warn "api_allowed_cidrs does not contain your current IP $my_ip"
    else
      ok "api_allowed_cidrs set"
    fi
  else
    warn "EKS API endpoint open to 0.0.0.0/0"
  fi
else
  bad "terraform.tfvars missing: cp terraform.tfvars.example terraform.tfvars"
fi

leftovers

echo
if (( failed == 0 )); then echo "Preflight passed."; else echo "Preflight found blocking problems (FAIL above)."; fi
exit "$failed"
