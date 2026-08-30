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
- CloudWatch alarms publish through SNS to an encrypted, fourteen-day SQS alarm
  inbox. Add an email, chat, or incident-management subscription when the team
  has an approved destination; the queue prevents alerts from being discarded
  in the meantime.
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
rule. Set `ReviewEndpointMode=cloudfront` after the AWS account is permitted to
create CloudFront distributions. CloudFront uses its default TLS certificate
and an origin-verification header; the internet-facing ALB rejects direct
requests. If CloudFront account verification is unavailable, set
`ReviewEndpointMode=apigateway`. That mode uses the API Gateway default HTTPS
endpoint, a VPC link, and an internal ALB. The VM remains unreachable directly
in both modes. For a custom domain, use an ACM certificate and HTTPS listener.

The generated Reka secret is deliberately a placeholder. Replace it with a
newly rotated key before writing the container secret file. Never reuse a key
that has appeared in chat or shell history.

Provision `review2-identity.yml` to create the Cognito issuer used by the API.
The pre-token hook validates the user's immutable JSON membership document
stored in `custom:tenant_memberships` and copies it into the signed
`tenant_memberships` claim. Use the stack outputs for `OIDC_ISSUER`,
`OIDC_AUDIENCE`, and `OIDC_JWKS_URL`. Demo users may be created only with
synthetic tenant identifiers; do not store email addresses, camera credentials,
or other personal information in membership claims.
The same stack provisions a Cognito managed-login domain with authorization
code plus PKCE. Pass the public review URL as both `CallbackUrl` and
`LogoutUrl`, then inject the `CognitoDomain` and `UserPoolClientId` outputs as
the web image build arguments. Teammates receive individual application
accounts; they never receive `REKA_API_KEY` or AWS credentials.

Provision `review2-operator.yml` before routine CLI work. It adds the existing
operator user to a self-service MFA group and permits only MFA-authenticated,
one-hour assumption of `CrimePredictionDeploymentRole`. The role has
`PowerUserAccess` plus IAM access limited to project-prefixed runtime roles; it
cannot administer account users. Do not create a long-lived access key for the
operator.

After the host is online and EFS is mounted:

Materialize runtime secrets with ownership matching the unprivileged image
user. Keep the directory root-only and never place secret values in the env
file or shell history:

```bash
install -d -o root -g root -m 0700 /opt/crime-platform/secrets
install -o 10001 -g 10001 -m 0400 /secure/input/database-url \
  /opt/crime-platform/secrets/database-url
install -o 10001 -g 10001 -m 0400 /secure/input/reka-api-key \
  /opt/crime-platform/secrets/reka-api-key
```

In AWS, retrieve each value from Secrets Manager directly into a protected
temporary file, install it as above, and securely remove the temporary file.
The numeric owner is the fixed `crime` runtime identity in the API image.

```bash
cp deploy/aws-vm/.env.production.example deploy/aws-vm/.env.production
chmod 600 deploy/aws-vm/.env.production
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml config
docker compose --env-file deploy/aws-vm/.env.production \
  -f deploy/aws-vm/compose.yml up -d --build
```

The host bootstrap installs checksum-pinned Docker Compose and Buildx plugins.
Both are required before the production multi-stage image build.

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
