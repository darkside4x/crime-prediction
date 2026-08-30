# AWS VM deployment

This deployment runs the production dependency graph on one EC2 host while
using managed PostgreSQL/RDS, S3/KMS, SQS/DLQ, Secrets Manager and an external
OIDC provider. It is appropriate for the hackathon and can scale API processes
and each worker stage independently. Multi-host production should move the same
containers to ECS/Fargate and mount the model registry from EFS or replace it
with a transactional artifact registry.

The review-two foundation provisions encrypted EFS and the production Compose
stack requires it at `MODEL_REGISTRY_HOST_PATH`. A long-running `freshclam`
service updates the shared malware definitions twice daily; API replicas mount
those definitions read-only and fail closed while scanning uploads.

## Required AWS controls

- EC2 has no inbound database, API or Docker daemon ports. Put an ALB or an
  HTTPS reverse proxy in front of port 8080; keep `WEB_BIND_ADDRESS=127.0.0.1`
  when the proxy runs on the same host.
- RDS is private, encrypted, backed up, and connects with a non-superuser
  `crime_app` role. Run migrations as that role so `FORCE ROW LEVEL SECURITY`
  remains effective.
- The S3 bucket has Block Public Access and Bucket Owner Enforced enabled, uses
  the configured customer KMS key, denies non-TLS access, and expires current
  and noncurrent media versions according to the retention policy.
- The SQS source queue has a redrive policy to the configured DLQ. The DLQ
  retention is longer than the source queue retention. Alarm on visible DLQ
  messages, oldest-message age, and in-flight saturation.
- The instance role—not static AWS keys—has only `s3:GetObject/PutObject/DeleteObject`
  on `tenants/*`, required KMS operations, SQS operations on these two queues,
  and `secretsmanager:GetSecretValue` under `LOCATION_SECRET_PREFIX`.
- The Reka key and PostgreSQL DSN are root-owned `0400` files under
  `/opt/crime-platform/secrets`; they are mounted as Compose secrets and never
  stored in the image or ordinary container environment.
- Mount the foundation EFS access point with TLS and IAM authorization at the
  configured `MODEL_REGISTRY_HOST_PATH` before starting Compose.

## Start

Provision the review foundation with CloudFormation (choose three subnets in
different availability zones):

```bash
aws cloudformation deploy \
  --region ap-south-1 \
  --stack-name crime-prediction-review2-foundation \
  --template-file deploy/aws-vm/review2-foundation.yml \
  --parameter-overrides \
    EnvironmentName=review2 \
    VpcId=vpc-replace-me \
    DatabaseSubnetIds=subnet-one,subnet-two,subnet-three \
    AllowDatabaseBootstrap=true \
  --capabilities CAPABILITY_IAM
```

Run `bootstrap-database.sh` once on the SSM-managed host using the stack's
database address and secret ARN outputs. Then immediately redeploy the
foundation with `AllowDatabaseBootstrap=false`. That removes the host's
temporary master-secret read and app-secret write permissions; normal runtime
can read only the sealed application DSN.

The host template defaults to a private SSM-managed EC2 host with no inbound
rule. Set `CreateReviewEndpoint=true` only after the AWS account is permitted to
create CloudFront distributions. That endpoint uses the CloudFront default TLS
certificate and an origin-verification header; the ALB rejects direct requests.
For a custom domain, use an ACM certificate and HTTPS listener instead.

The generated Reka secret is deliberately a placeholder. Replace it with a
newly rotated key before writing the container secret file. Never reuse a key
that has appeared in chat or shell history.

After the host is online and EFS is mounted:

```bash
cp deploy/aws-vm/.env.production.example deploy/aws-vm/.env.production
chmod 600 deploy/aws-vm/.env.production
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml config
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml up -d --build
```

Scale expensive stages independently:

```bash
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml up -d \
  --scale worker-upload=2 --scale worker-index=2 --scale worker-analyze=4
```

Do not scale the delete worker aggressively. Deletion is idempotent and
durable, but an operator should investigate any DLQ item before redrive.

## Readiness checks

```bash
curl --fail --silent http://127.0.0.1:8080/health
curl --fail --silent http://127.0.0.1:8080/ready
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml ps
```

`/ready` must not be treated as a load-balancer success if it reports a
degraded Reka state. Run the two-tenant database RLS test and a synthetic MP4
flow before opening the demo endpoint.
