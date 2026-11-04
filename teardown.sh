#!/usr/bin/env bash
# Deletes what deploy.sh made in AWS: the server box (and the database on it), the stack and its SSM
# settings. The R2 bucket, its parsed replays and its database backup stay.
set -euo pipefail
export AWS_PROFILE=${DEPLOY_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1} MSYS_NO_PATHCONV=1
STACK=${STACK:-bar-rewind-server}
aws cloudformation delete-stack --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --stack-name "$STACK"
for name in $(aws ssm get-parameters-by-path --path "/$STACK" --query 'Parameters[].Name' --output text); do
  aws ssm delete-parameter --name "$name"
done
echo "deleted $STACK"
