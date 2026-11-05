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
# Each inbound queue gets a <name>-dlq and a redrive policy of five receives (LLD §12.4). The policy
# is set after creation, so a rerun against a persisted volume (queue already there) still works.
for queue in llm-document-rag-q domainprofile-llm-invalidate-q membership-rag-q \
             tender-rag-q usage-rag-q; do
  aws sqs create-queue --queue-name "$queue-dlq" >/dev/null
  url=$(aws sqs create-queue --queue-name "$queue" --query QueueUrl --output text)
  policy=$(printf '{\\"deadLetterTargetArn\\":\\"arn:aws:sqs:eu-west-1:000000000000:%s-dlq\\",\\"maxReceiveCount\\":\\"5\\"}' "$queue")
  aws sqs set-queue-attributes --queue-url "$url" --attributes "{\"RedrivePolicy\":\"$policy\"}"
done
# Dev probe: a queue subscribed to both outbound topics (raw delivery), so published events can be
# seen in floci-ui / `aws sqs receive-message`. Nothing in the service consumes it.
aws sqs create-queue --queue-name llm-events-probe-q >/dev/null
for topic in llm-generation-events llm-index-events; do
  aws sns subscribe --topic-arn "arn:aws:sns:eu-west-1:000000000000:$topic" --protocol sqs \
    --notification-endpoint arn:aws:sqs:eu-west-1:000000000000:llm-events-probe-q \
    --attributes RawMessageDelivery=true >/dev/null
done
echo "floci: rag-artefacts bucket, 2 topics, 5 queues with DLQs ready"
