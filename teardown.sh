#!/usr/bin/env bash
# Deletes everything deploy.sh made: the parsed replays, the stack and its SSM settings.
set -euo pipefail
export AWS_PROFILE=${DEPLOY_PROFILE:-personal} AWS_REGION=${AWS_REGION:-us-east-1} MSYS_NO_PATHCONV=1
STACK=${STACK:-bar-rewind}
BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
# CloudFormation cannot delete a bucket with objects in it
aws s3 rm "s3://$BUCKET" --recursive >/dev/null
aws cloudformation delete-stack --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --stack-name "$STACK"
for name in session-key origin-secret cloudfront-private-key discord-client-secret site-url; do
  aws ssm delete-parameter --name "/$STACK/$name" 2>/dev/null || true
done
echo "deleted $STACK"
