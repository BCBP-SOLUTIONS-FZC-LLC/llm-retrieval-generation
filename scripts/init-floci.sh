#!/bin/sh
# Runs inside floci once it is ready (/etc/floci/init/ready.d). Creates the S3 bucket and the
# SNS topics / SQS queues named in src/rag/infrastructure/config.py. Idempotent.
set -eu
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=eu-west-1
aws() { command aws --region eu-west-1 --endpoint-url http://localhost:4566 "$@"; }

aws s3api head-bucket --bucket rag-artefacts 2>/dev/null || aws s3api create-bucket \
  --bucket rag-artefacts --create-bucket-configuration LocationConstraint=eu-west-1

for topic in llm-generation-events llm-index-events; do
  aws sns create-topic --name "$topic" >/dev/null
done
for queue in llm-document-rag-q domainprofile-llm-invalidate-q iam-rag-purge-q \
             iam-rag-membership-q tender-rag-q usage-rag-q; do
  aws sqs create-queue --queue-name "$queue" >/dev/null
done
# Dev probe: a queue subscribed to both outbound topics (raw delivery), so published events can be
# seen in floci-ui / `aws sqs receive-message`. Nothing in the service consumes it.
aws sqs create-queue --queue-name llm-events-probe-q >/dev/null
for topic in llm-generation-events llm-index-events; do
  aws sns subscribe --topic-arn "arn:aws:sns:eu-west-1:000000000000:$topic" --protocol sqs \
    --notification-endpoint arn:aws:sqs:eu-west-1:000000000000:llm-events-probe-q \
    --attributes RawMessageDelivery=true >/dev/null
done
echo "floci: rag-artefacts bucket, 2 topics, 6 queues ready"
