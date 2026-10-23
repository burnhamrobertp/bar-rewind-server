#!/usr/bin/env bash
# Runs the parse worker on this machine under Docker, after deploy.sh built its image:
#   ./local-worker.sh          (re)start it; it keeps running, and Docker restarts it with Docker Desktop
#   ./local-worker.sh stop     stop it (Fargate takes the queue back within about 2 minutes, unless deployed
#                              with CLOUD_WORKER=off)
#   ./local-worker.sh key      make a new access key for it, replacing the old one (rotation)
#   ./local-worker.sh widgets <folder>   (re)start it with the .lua widgets in <folder> running in every
#                                        parse; remembered, "none" forgets it
# Its access key lives in ~/.aws/bar-rewind-worker, the only credentials the container can see.
# Engines, games and maps stay in the Docker volume bar-rewind-bar.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE=${DEPLOY_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1} MSYS_NO_PATHCONV=1
STACK=${STACK:-bar-rewind}
NAME=bar-rewind-worker
KEYFILE="$HOME/.aws/bar-rewind-worker"
WIDGETS_FILE="$HOME/.aws/bar-rewind-worker-widgets"
output() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

new_key() {
  local user old
  user=$(output LocalWorkerUser)
  old=$(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text)
  mkdir -p "$(dirname "$KEYFILE")"
  # straight into the file: the secret is never printed
  # tr: the Windows CLI ends its lines in \r, which would end up in the secret
  aws iam create-access-key --user-name "$user" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | tr -d '\r' |
    { read -r id secret; printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "$id" "$secret" > "$KEYFILE"; }
  chmod 600 "$KEYFILE"
  for k in $old; do aws iam delete-access-key --user-name "$user" --access-key-id "$k"; done
  echo "new access key for $user in $KEYFILE"
}

case "${1:-start}" in
  stop) docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "stopped"; exit 0 ;;
  key) new_key; [ -z "$(docker ps -q -f name="^$NAME$")" ] && exit 0 ;;
  start) [ -f "$KEYFILE" ] || new_key ;;
  widgets)
    if [ "${2:-}" = none ]; then rm -f "$WIDGETS_FILE"
    else [ -d "${2:-}" ] || { echo "usage: local-worker.sh widgets <folder>|none" >&2; exit 2; }; (cd "$2" && pwd) > "$WIDGETS_FILE"; fi
    [ -f "$KEYFILE" ] || new_key ;;
  *) echo "usage: local-worker.sh [start|stop|key|widgets <folder>|widgets none]" >&2; exit 2 ;;
esac

widgets=()
if [ -f "$WIDGETS_FILE" ]; then
  dir=$(cat "$WIDGETS_FILE")
  widgets=(-v "$(cygpath -w "$dir" 2>/dev/null || echo "$dir"):/widgets:ro")
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped --stop-timeout 60 \
  -e STAY=1 -e TABLE="$(output TableName)" -e BUCKET="$(output BucketName)" -e AWS_DEFAULT_REGION="$AWS_REGION" \
  -v "$(cygpath -w "$KEYFILE" 2>/dev/null || echo "$KEYFILE"):/root/.aws/credentials:ro" \
  "${widgets[@]}" -v bar-rewind-bar:/bar "$NAME:latest" >/dev/null
echo "local worker running ($(docker ps --filter name="^$NAME$" --format '{{.Status}}'))"
