#!/usr/bin/env bash
# Deploys or updates the BAR Rewind server at https://$DOMAIN/$BASE_PATH/: ./deploy.sh <email for cost alerts>
# Needs the AWS CLI, Docker, OpenSSL, Python 3, Zig 0.17 on PATH and a BAR-Rewind checkout at $BAR_REWIND
# (default ../BAR-Rewind). The Discord client secret goes in SSM as /$STACK/discord-client-secret
# (SecureString) before the first sign-in; DISCORD_CLIENT_SECRET sets it here instead. AWS_PROFILE defaults
# to "personal", AWS_REGION to us-east-1 (CloudFront certificates must be there).
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE=${AWS_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1}
# Git Bash would rewrite /bar-rewind/... arguments into Windows paths
export MSYS_NO_PATHCONV=1
STACK=${STACK:-bar-rewind}
DOMAIN=${DOMAIN:-robotofham.com}
BASE_PATH=${BASE_PATH:-bar-rewind}
BAR_REWIND=${BAR_REWIND:-../BAR-Rewind}
PYTHON=${PYTHON:-$(command -v python3 || command -v python)}
EMAIL=${1:?usage: deploy.sh <email for cost alerts>}
mkdir -p .deploy

param() { aws ssm get-parameter --name "/$STACK/$1" --with-decryption --query Parameter.Value --output text 2>/dev/null || true; }
secret() { aws ssm put-parameter --name "/$STACK/$1" --type SecureString --overwrite --value "$2" >/dev/null; }
output() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "== secrets"
[ -n "$(param session-key)" ] || secret session-key "$(openssl rand -hex 32)"
[ -n "$(param origin-secret)" ] || secret origin-secret "$(openssl rand -hex 24)"
[ -n "$(param cloudfront-private-key)" ] || secret cloudfront-private-key "$(openssl genrsa 2048 2>/dev/null)"
if [ -n "${DISCORD_CLIENT_SECRET:-}" ]; then secret discord-client-secret "$DISCORD_CLIENT_SECRET"
elif [ -z "$(param discord-client-secret)" ]; then secret discord-client-secret unset; fi
param cloudfront-private-key > .deploy/key.pem
PUBLIC_KEY=$(openssl rsa -in .deploy/key.pem -pubout 2>/dev/null)
rm .deploy/key.pem

echo "== certificate for $DOMAIN"
CERT=$(aws acm list-certificates --certificate-statuses ISSUED PENDING_VALIDATION \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
if [ "$CERT" = "None" ]; then
  CERT=$(aws acm request-certificate --domain-name "$DOMAIN" --validation-method DNS --query CertificateArn --output text)
  sleep 10
fi
if [ "$(aws acm describe-certificate --certificate-arn "$CERT" --query Certificate.Status --output text)" != "ISSUED" ]; then
  echo "Add this DNS record in Cloudflare (DNS only), then wait here:"
  aws acm describe-certificate --certificate-arn "$CERT" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Type,Name,Value]' --output text
  aws acm wait certificate-validated --certificate-arn "$CERT"
fi

echo "== stack"
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
TAG=$(date -u +%Y%m%d%H%M%S)
aws cloudformation deploy --stack-name "$STACK" --template-file template.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset --parameter-overrides \
  "SiteDomain=$DOMAIN" "BasePath=$BASE_PATH" "CertificateArn=$CERT" "VpcId=$VPC" "SubnetIds=$SUBNETS" \
  "CloudFrontPublicKey=$PUBLIC_KEY" "OriginSecret=$(param origin-secret)" "AlertEmail=$EMAIL" "ImageTag=$TAG"
BUCKET=$(output BucketName)
REPO=$(output RepositoryUri)
aws ssm put-parameter --name "/$STACK/site-url" --type String --overwrite --value "https://$DOMAIN/$BASE_PATH" >/dev/null

echo "== BAR-Rewind (the player for $DOMAIN, the Linux package for the worker)"
(cd "$BAR_REWIND" && SERVER_HOST=$DOMAIN "$PYTHON" tools/dist.py)
cp "$BAR_REWIND/dist/bar-rewind-linux.tar.gz" worker/
cp "$BAR_REWIND/dist/bar-rewind.exe" site/

echo "== worker image $TAG"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}" >/dev/null
docker build -q --platform linux/amd64 -t "$REPO:$TAG" worker >/dev/null
docker push -q "$REPO:$TAG" >/dev/null

echo "== API"
"$PYTHON" -c "import zipfile; zipfile.ZipFile('.deploy/api.zip', 'w', zipfile.ZIP_DEFLATED).write('api/app.py', 'app.py')"
aws lambda update-function-code --function-name "$(output ApiFunction)" --zip-file fileb://.deploy/api.zip >/dev/null
aws lambda wait function-updated --function-name "$(output ApiFunction)"

echo "== site"
# the parsed replays share the prefix: --delete must not reach them
aws s3 sync site/ "s3://$BUCKET/$BASE_PATH/" --delete --exclude "curves/*" >/dev/null

echo
echo "https://$DOMAIN/$BASE_PATH/"
echo "Cloudflare: $DOMAIN CNAME $(output CloudFrontHost), DNS only (replacing the old A/AAAA records)"
echo "Discord OAuth2 redirect: https://$DOMAIN/$BASE_PATH/api/callback"
