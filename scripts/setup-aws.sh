#!/usr/bin/env bash
# Provision AWS for a mybrew instance: a public-read S3 bucket for bottles and
# an IAM role that only the instance repo's main branch can assume via GitHub
# OIDC. No long-lived AWS keys are created. Safe to re-run.
#
# Usage:
#   setup-aws.sh --repo OWNER/homebrew-mybrew --bucket NAME [--region R] [--profile P] [--role NAME] [--check]
#
#   --check   read-only: verify access and show what would be created, change nothing
#
# Afterwards, sets MYBREW_S3_BUCKET, MYBREW_AWS_REGION, MYBREW_AWS_ROLE_ARN and
# MYBREW_ROOT_URL as repository variables (not secrets) on the instance repo via gh.

set -euo pipefail

REGION=us-east-1
ROLE=mybrew-publisher
PREFIX=bottles
REPO="" BUCKET="" PROFILE_ARGS=() CHECK=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE_ARGS=(--profile "$2"); shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    --check) CHECK=true; shift ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
  esac
done
[[ -n "$REPO" && -n "$BUCKET" ]] || { sed -n '2,13p' "$0"; exit 2; }

OIDC_HOST=token.actions.githubusercontent.com
ROOT_URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/${PREFIX}/sequoia"

aws() { command aws "${PROFILE_ARGS[@]}" --region "$REGION" --output text "$@"; }
step() { printf '\n==> %s\n' "$*"; }

# Classify an AWS call: ok | missing | denied. Anything else aborts.
probe() {
  local out
  if out=$(aws "$@" 2>&1); then echo ok; return; fi
  case "$out" in
    *AccessDenied*|*UnauthorizedOperation*|*"not authorized"*) echo denied ;;
    *NoSuchEntity*|*"Not Found"*|*NoSuchBucket*|*"(404)"*) echo missing ;;
    *) echo "$out" >&2; exit 1 ;;
  esac
}

step "Identity"
ACCOUNT=$(aws sts get-caller-identity --query Account)
aws sts get-caller-identity --query Arn
PROVIDER_ARN="arn:aws:iam::${ACCOUNT}:oidc-provider/${OIDC_HOST}"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

step "Preflight"
provider=$(probe iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN")
role=$(probe iam get-role --role-name "$ROLE")
bucket=$(probe s3api head-bucket --bucket "$BUCKET")
printf '  OIDC provider %-45s %s\n' "$OIDC_HOST" "$provider"
printf '  IAM role      %-45s %s\n' "$ROLE" "$role"
printf '  S3 bucket     %-45s %s\n' "$BUCKET" "$bucket"

if [[ "$provider" == denied || "$role" == denied || "$bucket" == denied ]]; then
  cat >&2 <<EOF

The caller cannot read some of these resources, so it almost certainly cannot
create them. Run this script with a profile that has, at minimum:
  iam:GetOpenIDConnectProvider, iam:CreateOpenIDConnectProvider,
  iam:GetRole, iam:CreateRole, iam:UpdateAssumeRolePolicy, iam:PutRolePolicy,
  s3:CreateBucket, s3:PutBucketPublicAccessBlock, s3:PutBucketPolicy,
  s3:PutEncryptionConfiguration, s3:ListBucket
EOF
  exit 1
fi

if $CHECK; then
  printf '\nCheck only. Would publish bottles to %s\nand let %s (main branch) assume %s.\n' "$ROOT_URL" "$REPO" "$ROLE_ARN"
  exit 0
fi

step "GitHub OIDC provider"
if [[ "$provider" == missing ]]; then
  aws iam create-open-id-connect-provider --url "https://${OIDC_HOST}" --client-id-list sts.amazonaws.com >/dev/null
  echo "created $PROVIDER_ARN"
else
  echo "exists"
fi

step "S3 bucket"
if [[ "$bucket" == missing ]]; then
  if [[ "$REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  echo "created s3://$BUCKET"
else
  echo "exists"
fi
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
# ACLs stay blocked; public read comes only from the bucket policy below.
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadBottles",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::${BUCKET}/${PREFIX}/*"
  }]
}
EOF
)"
echo "public read on s3://$BUCKET/$PREFIX/*"

step "IAM role"
TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${PROVIDER_ARN}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "${OIDC_HOST}:aud": "sts.amazonaws.com",
        "${OIDC_HOST}:sub": "repo:${REPO}:ref:refs/heads/main"
      }
    }
  }]
}
EOF
)
if [[ "$role" == missing ]]; then
  aws iam create-role --role-name "$ROLE" --max-session-duration 3600 \
    --description "mybrew: upload bottles from ${REPO}" \
    --assume-role-policy-document "$TRUST" >/dev/null
  echo "created $ROLE_ARN"
else
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
  echo "trust policy updated"
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name upload-bottles --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject"],
    "Resource": "arn:aws:s3:::${BUCKET}/${PREFIX}/*"
  }]
}
EOF
)"
echo "role may only Put/Get s3://$BUCKET/$PREFIX/*"

step "Repository variables on $REPO"
gh variable set MYBREW_S3_BUCKET --repo "$REPO" --body "$BUCKET"
gh variable set MYBREW_AWS_REGION --repo "$REPO" --body "$REGION"
gh variable set MYBREW_AWS_ROLE_ARN --repo "$REPO" --body "$ROLE_ARN"
gh variable set MYBREW_ROOT_URL --repo "$REPO" --body "$ROOT_URL"

printf '\nDone. Bottles will be served from %s\n' "$ROOT_URL"
