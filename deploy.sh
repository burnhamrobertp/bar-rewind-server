#!/usr/bin/env bash
# Deploys or updates the BAR Rewind server: ./deploy.sh <email for cost alerts>
# Ships the commit checked out here, which must be pushed: the server fetches it from GitHub.
# Needs the AWS CLI and OpenSSL. The first run also needs, in the environment (each is kept in SSM after):
#   DISCORD_CLIENT_SECRET   the Discord application's secret
#   CLOUDFLARED_TOKEN       the token of a Cloudflare Tunnel whose public hostname is $DOMAIN -> http://localhost:8080
#   R2_ENDPOINT             https://<account>.eu.r2.cloudflarestorage.com (no .eu for a bucket outside the EU jurisdiction)
#   R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY   an R2 bucket and an Object Read & Write token for it
# DEPLOY_PROFILE picks the AWS profile (default "personal"), AWS_REGION defaults to us-east-1.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE=${DEPLOY_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1}
# Git Bash would rewrite /bar-rewind-server/... arguments into Windows paths
export MSYS_NO_PATHCONV=1
# the AWS CLI on Windows can't print systemctl's status dots in the console's code page
export PYTHONUTF8=1
STACK=${STACK:-bar-rewind-server}
DOMAIN=${DOMAIN:-bar-rewind.robotofham.com}
DISCORD_CLIENT_ID=${DISCORD_CLIENT_ID:-1562509970933555241}
GUILD_ID=${GUILD_ID:-549281623154229250}
# who may sign in: members of the guild with one of these roles, or these users; admins pin releases
ROLE_IDS=${ROLE_IDS:-549291872192036874}
USER_IDS=${USER_IDS:-183418851067822085}
ADMIN_IDS=${ADMIN_IDS:-183418851067822085}
EMAIL=${1:?usage: deploy.sh <email for cost alerts>}

COMMIT=$(git rev-parse HEAD)
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit or stash your changes first: the server runs $COMMIT" >&2; exit 1; }
git fetch -q origin
git branch -r --contains "$COMMIT" | grep -q . || { echo "push $COMMIT first: the server fetches it from GitHub" >&2; exit 1; }

has() {
  local out
  out=$(aws ssm get-parameter --name "/$STACK/$1" --query Parameter.Name --output text 2>&1) && return 0
  grep -q ParameterNotFound <<<"$out" && return 1
  echo "$out" >&2
  exit 1
}
put() { aws ssm put-parameter --name "/$STACK/$1" --type "$2" --overwrite --value "$3" >/dev/null; }
secret() {
  if [ -n "${2:-}" ]; then put "$1" SecureString "$2"
  elif ! has "$1"; then echo "set $3 for the first deploy" >&2; exit 1; fi
}

echo "== AWS account $(aws sts get-caller-identity --query Account --output text) (profile $AWS_PROFILE)"
echo "== settings"
has session-key || put session-key SecureString "$(openssl rand -hex 32)"
secret discord-client-secret "${DISCORD_CLIENT_SECRET:-}" DISCORD_CLIENT_SECRET
secret cloudflared-token "${CLOUDFLARED_TOKEN:-}" CLOUDFLARED_TOKEN
secret r2-access-key-id "${R2_ACCESS_KEY_ID:-}" R2_ACCESS_KEY_ID
secret r2-secret-access-key "${R2_SECRET_ACCESS_KEY:-}" R2_SECRET_ACCESS_KEY
[ -n "${R2_ENDPOINT:-}" ] && put r2-endpoint String "$R2_ENDPOINT"
[ -n "${R2_BUCKET:-}" ] && put r2-bucket String "$R2_BUCKET"
{ has r2-endpoint && has r2-bucket; } || { echo "set R2_ENDPOINT and R2_BUCKET for the first deploy" >&2; exit 1; }
put site-url String "https://$DOMAIN"
put discord-client-id String "$DISCORD_CLIENT_ID"
put guild-id String "$GUILD_ID"
put role-ids String "$ROLE_IDS"
put user-ids String "$USER_IDS"
put admin-ids String "$ADMIN_IDS"

echo "== stack"
if [ "$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null)" = "ROLLBACK_COMPLETE" ]; then
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
fi
# a new Commit or ImageId changes the box itself, which replaces it: both keep their first values
previous() {
  aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" \
    --output text 2>/dev/null || true
}
FIRST_COMMIT=$(previous Commit)
IMAGE=$(previous ImageId)
[ -n "$IMAGE" ] || IMAGE=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)
aws cloudformation deploy --stack-name "$STACK" --template-file deploy/template.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset --tags "project=$STACK" --parameter-overrides "AlertEmail=$EMAIL" \
  "Commit=${FIRST_COMMIT:-$COMMIT}" "ImageId=$IMAGE"
INSTANCE=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)

echo "== $COMMIT onto $INSTANCE"
for _ in $(seq 60); do
  [ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE" --query 'InstanceInformationList[0].PingStatus' --output text)" = "Online" ] && break
  sleep 10
done
# a fresh box's first boot may still be cloning and installing
COMMAND=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript --comment "deploy $COMMIT" --timeout-seconds 900 \
  --parameters "commands=[\"cloud-init status --wait >/dev/null || true\",\"set -e\",\"cd /opt/bar-rewind-server\",\"git fetch -q origin\",\"git checkout -q --detach $COMMIT\",\"PARAMS=/$STACK AWS_DEFAULT_REGION=$AWS_REGION deploy/install.sh\"]" \
  --query Command.CommandId --output text)
STATUS=InProgress
for _ in $(seq 90); do
  STATUS=$(aws ssm get-command-invocation --command-id "$COMMAND" --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
  case $STATUS in Pending|InProgress|Delayed) sleep 10 ;; *) break ;; esac
done
aws ssm get-command-invocation --command-id "$COMMAND" --instance-id "$INSTANCE" --query '[StandardOutputContent, StandardErrorContent]' --output text | tail -40
[ "$STATUS" = Success ] || { echo "the deploy on $INSTANCE ended $STATUS" >&2; exit 1; }

echo
echo "https://$DOMAIN/"
echo "Discord OAuth2 redirect: https://$DOMAIN/api/callback"
echo "R2: a lifecycle rule deleting curves/ and logs/ after 60 days (the dashboard; the deploy's token can't set one)"
