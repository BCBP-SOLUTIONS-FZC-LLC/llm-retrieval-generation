# IRSA role policy for llm-retrieval-generation

The reference policy for the service's IRSA role. The application does not create the role: platform Terraform owns it. Copy `policy.json` into the Terraform module (it is a `templatefile` with `${...}` placeholders) or apply it with `aws iam put-role-policy`, then wire the role ARN into Helm:

```yaml
serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT_ID:role/llm-retrieval-generation
```

The API pods, the migration Job and every sweep CronJob share that ServiceAccount, so they share the role. The pods carry no static AWS keys.

## Grants

| Sid | Service | Actions | Purpose |
|---|---|---|---|
| `PublishGenerationAndIndexEvents` | SNS | `Publish`, `GetTopicAttributes` | The routing publisher sends ten event types to `llm-generation-events` and seven to `llm-index-events` (`GENERATION_TOPIC_ARN`, `INDEX_TOPIC_ARN`). Both topics are needed. |
| `ConsumeEventQueues` | SQS | `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`, `GetQueueUrl`, `ChangeMessageVisibility`, `SendMessage` | The six consumers (document, domain profile, IAM purge, IAM membership, Tender, usage) and their dead-letter queues. eventcommon's DLQ publisher reads each queue's redrive policy (`GetQueueAttributes`), resolves the DLQ (`GetQueueUrl`) and forwards poison messages to it (`SendMessage`). |
| `ArtefactContentObjects`, `ArtefactContentListForPrefixDelete` | S3 | `GetObject`, `PutObject`, `DeleteObject`; `ListBucket` | The artefact content store: tenant-prefixed keys, a batched prefix delete for tenant purge. |
| `ArtefactContentEncryption` | KMS | `GenerateDataKey`, `Decrypt` | SSE-KMS on those objects with the tenant in the encryption context (`S3_KMS_KEY_ID`). |

The queue and topic names above are the ones the LLD and `scripts/init-floci.sh` define; the account-level ARNs are Terraform's.

## Explicitly out of scope

- **No Glue Schema Registry.** The event schemas are derived JSON Schemas in `schemas/events/`, registered by the platform's schema governance tooling (`make schema-validate`), not by this role.
- **No SQS or SNS administration** (no create, delete, purge or policy changes), and no `s3:DeleteBucket` or lifecycle changes.
- **No Bedrock or any model access.** Model calls go through the Model Gateway, which owns credentials and residency. A `bedrock:*` grant here would mean the service is bypassing the Gateway.
- **No database credentials.** Those are in the Secret (`existingSecret`), not IAM.

## Rolling out

1. Apply the policy and attach it to the role.
2. From a debug pod using the same ServiceAccount:
   ```bash
   aws sns get-topic-attributes --topic-arn <generation_events_topic_arn>   # expect Attributes
   aws sqs get-queue-attributes --queue-url <document_queue_url> --attribute-names All
   aws s3api put-object --bucket <bucket> --key _probe/x --body /dev/null \
       --server-side-encryption aws:kms --ssekms-key-id <key>              # expect an ETag
   aws s3api delete-object --bucket <bucket> --key _probe/x
   ```
3. Deploy the image and chart (`helm upgrade --install ... -f values.yaml -f values-<env>.yaml`).
4. Watch the relay and consumer logs for `AccessDenied`. A rising `platform_dlq_messages_total` after the rollout (once metrics exist) means a grant is missing.

## Least privilege, still to do

The topic and queue resource policies should name this role and the other subscribers only. That is platform infrastructure and is not represented here. The KMS grant can also be narrowed with a `kms:EncryptionContext` condition once the encryption-context key names are agreed with the platform team.
