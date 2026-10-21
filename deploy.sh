#!/usr/bin/env bash
# Deploys or updates the BAR Rewind server at https://$DOMAIN: ./deploy.sh <email for cost alerts>
# Needs the AWS CLI, Docker, OpenSSL, Python 3, Zig 0.17 on PATH and a BAR-Rewind checkout at $BAR_REWIND
# (default ../BAR-Rewind). The Discord client secret goes in SSM as /$STACK/discord-client-secret
# (SecureString) before the first sign-in; DISCORD_CLIENT_SECRET sets it here instead. DEPLOY_PROFILE picks
# the AWS profile (default "personal"; a shell's own AWS_PROFILE is ignored), AWS_REGION defaults to us-east-1
# (CloudFront certificates must be there).
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE=${DEPLOY_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1}
# Git Bash would rewrite /bar-rewind/... arguments into Windows paths
export MSYS_NO_PATHCONV=1
STACK=${STACK:-bar-rewind}
DOMAIN=${DOMAIN:-bar-rewind.robotofham.com}
BAR_REWIND=${BAR_REWIND:-../BAR-Rewind}
# the first that runs: on Windows python3 can be the Microsoft Store's placeholder
for candidate in ${PYTHON:-} python3 python; do
  "$candidate" -c "" 2>/dev/null && PYTHON=$candidate && break
done
EMAIL=${1:?usage: deploy.sh <email for cost alerts>}
mkdir -p .deploy

param() { aws ssm get-parameter --name "/$STACK/$1" --with-decryption --query Parameter.Value --output text; }
# false only when the parameter does not exist; any other failure (an expired sign-in) stops the
# script rather than passing for a missing secret that then gets regenerated
has() {
  local out
  out=$(aws ssm get-parameter --name "/$STACK/$1" --query Parameter.Name --output text 2>&1) && return 0
  grep -q ParameterNotFound <<<"$out" && return 1
  echo "$out" >&2
  exit 1
}
secret() { aws ssm put-parameter --name "/$STACK/$1" --type SecureString --overwrite --value "$2" >/dev/null; }
output() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "== AWS account $(aws sts get-caller-identity --query Account --output text) (profile $AWS_PROFILE)"
echo "== secrets"
has session-key || secret session-key "$(openssl rand -hex 32)"
has origin-secret || secret origin-secret "$(openssl rand -hex 24)"
has cloudfront-private-key || secret cloudfront-private-key "$(openssl genrsa 2048 2>/dev/null)"
if [ -n "${DISCORD_CLIENT_SECRET:-}" ]; then secret discord-client-secret "$DISCORD_CLIENT_SECRET"
elif ! has discord-client-secret; then secret discord-client-secret unset; fi
ORIGIN_SECRET=$(param origin-secret)
PRIVATE_KEY=$(param cloudfront-private-key)
PUBLIC_KEY=$(openssl rsa -pubout 2>/dev/null <<<"$PRIVATE_KEY")

echo "== certificate for $DOMAIN"
CERT=$(aws acm list-certificates --certificate-statuses ISSUED PENDING_VALIDATION \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
if [ "$CERT" = "None" ]; then
  CERT=$(aws acm request-certificate --domain-name "$DOMAIN" --validation-method DNS --tags "Key=project,Value=$STACK" \
    --query CertificateArn --output text)
  sleep 10
fi
if [ "$(aws acm describe-certificate --certificate-arn "$CERT" --query Certificate.Status --output text)" != "ISSUED" ]; then
  echo "Add this DNS record in Cloudflare (DNS only), then wait here:"
  aws acm describe-certificate --certificate-arn "$CERT" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Type,Name,Value]' --output text
  # ACM can take half an hour to look; the CLI's own waiter gives up sooner
  for _ in $(seq 180); do
    STATUS=$(aws acm describe-certificate --certificate-arn "$CERT" --query Certificate.Status --output text)
    [ "$STATUS" = "PENDING_VALIDATION" ] || break
    sleep 20
  done
  [ "$STATUS" = "ISSUED" ] || { echo "certificate is $STATUS" >&2; exit 1; }
fi

echo "== stack"
# ECS needs its service-linked role once per account before the first cluster
aws iam get-role --role-name AWSServiceRoleForECS >/dev/null 2>&1 ||
  aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null
# a first create that failed leaves a stack that can only be deleted
if [ "$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null)" = "ROLLBACK_COMPLETE" ]; then
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
fi
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
TAG=$(date -u +%Y%m%d%H%M%S)
CURVES_VERSION=$(sed -n 's/^pub const version = \([0-9]*\);.*/\1/p' "$BAR_REWIND/src/curves.zig")
[ -n "$CURVES_VERSION" ] || { echo "no curves version in $BAR_REWIND/src/curves.zig" >&2; exit 1; }
aws cloudformation deploy --stack-name "$STACK" --template-file template.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset --tags "project=$STACK" --parameter-overrides \
  "SiteDomain=$DOMAIN" "CertificateArn=$CERT" "VpcId=$VPC" "SubnetIds=$SUBNETS" \
  "CloudFrontPublicKey=$PUBLIC_KEY" "OriginSecret=$ORIGIN_SECRET" "AlertEmail=$EMAIL" "ImageTag=$TAG" "CurvesVersion=$CURVES_VERSION"
BUCKET=$(output BucketName)
REPO=$(output RepositoryUri)
aws ssm put-parameter --name "/$STACK/site-url" --type String --overwrite --value "https://$DOMAIN" >/dev/null

echo "== BAR-Rewind (the players, the Linux package for the worker)"
(cd "$BAR_REWIND" && "$PYTHON" tools/dist.py)
cp "$BAR_REWIND/dist/bar-rewind-linux.tar.gz" worker/
cp "$BAR_REWIND/dist/bar-rewind.exe" site/
cp "$BAR_REWIND/dist/bar-rewind-player-linux" site/bar-rewind-linux

echo "== worker image $TAG"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}" >/dev/null
docker build -q --platform linux/amd64 -t "$REPO:$TAG" worker >/dev/null
docker push -q "$REPO:$TAG" >/dev/null
# the same image for local-worker.sh, which a running local worker restarts on
docker tag "$REPO:$TAG" bar-rewind-worker:latest
if [ -n "$(docker ps -q -f name='^bar-rewind-worker$')" ]; then ./local-worker.sh; fi

echo "== API"
"$PYTHON" -c "import zipfile; zipfile.ZipFile('.deploy/api.zip', 'w', zipfile.ZIP_DEFLATED).write('api/app.py', 'app.py')"
aws lambda update-function-code --function-name "$(output ApiFunction)" --zip-file fileb://.deploy/api.zip >/dev/null
aws lambda wait function-updated --function-name "$(output ApiFunction)"

echo "== site"
aws s3 sync site/ "s3://$BUCKET/site/" --delete >/dev/null

echo
echo "https://$DOMAIN/"
echo "Cloudflare: $DOMAIN CNAME $(output CloudFrontHost), DNS only"
echo "Discord OAuth2 redirect: https://$DOMAIN/api/callback"
