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
  HTTPS reverse proxy in front of port 8080. The managed ALB requires
  `WEB_BIND_ADDRESS=0.0.0.0` because it targets the instance ENI; its ingress
  rule accepts only the load-balancer security group. Use `127.0.0.1` for an
  SSM-only deployment or when the reverse proxy runs on the same host.
- The application security group has no all-protocol egress. It permits only
  required HTTPS/build traffic and security-group-scoped PostgreSQL/EFS
  traffic. Prebuilt-image deployments should also remove port 80.
- The AWS composition does not run the CLI live-camera worker and does not
  permit direct RTSP/RTSPS egress. Tenant HLS/RTSP/ONVIF connectors remain
  disabled in production until they are routed through a pinned outbound media
  proxy or egress firewall that revalidates DNS, redirects, and every HLS child
  URI. Review 3 uses authenticated, bounded browser/mobile uploads instead.
- RDS is private, encrypted and backed up. The one-shot migration container uses
  the non-superuser schema owner `crime_migrator`; APIs and workers use the
  separate `crime_app` runtime role. `crime_app` owns no tables or functions,
  has no database/schema `CREATE`, role inheritance, `BYPASSRLS`, or policy
  replacement path, and receives only schema usage, application DML, sequence
  usage and execution of the `app` helper functions. All tenant tables continue
  to use `FORCE ROW LEVEL SECURITY`. The Postgres demo queue follows the same
  rule; its runtime mutations are tenant-scoped and its only cross-tenant
  operations are narrow claim/depth functions. Production worker delivery
  remains on SQS.
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
- The Reka key and separate runtime/migrator PostgreSQL DSNs are root-owned
  `0400` files under
  `/opt/crime-platform/secrets`; they are mounted as Compose secrets and never
  stored in the image or ordinary container environment.
- Voice dispatch has a separate encrypted SQS queue/DLQ and Twilio secret. It
  defaults to `DISPATCH_MODE=mock`, so no telephone call can leave the system.
  Live mode is permitted only after a human-confirmed incident, an explicit
  reviewer dispatch authorization, opted-in demo contacts, and valid Twilio
  webhook signatures. The policy is fixed at two primary attempts followed by
  one supervisor attempt; acknowledgement stops escalation.
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
database address plus `DatabaseMasterSecretArn`, `RuntimeDatabaseSecretArn` and
`MigratorDatabaseSecretArn` outputs. The script safely upgrades a legacy
single-role database by transferring objects from `crime_app` to
`crime_migrator`, installs future-object default privileges, verifies the
runtime role has no ownership or DDL capability, and seals separate DSNs.
Materialize both DSNs as protected files, then immediately redeploy the
foundation with `AllowDatabaseBootstrap=false`. That removes master/migrator
secret bootstrap access; normal host operation can retrieve only the sealed
runtime DSN. Retain the already materialized migrator file only on the protected
deployment host for the one-shot migration container.

The bootstrap inputs are identifiers, not credential values. Resolve them from
the stack outputs and run the script without shell tracing:

```bash
AWS_REGION=ap-south-1 \
DATABASE_HOST=db-private.example.internal \
DATABASE_NAME=crime_prediction \
DATABASE_MASTER_SECRET_ARN=arn:aws:secretsmanager:region:account:secret:master \
DATABASE_RUNTIME_SECRET_ARN=arn:aws:secretsmanager:region:account:secret:runtime \
DATABASE_MIGRATOR_SECRET_ARN=arn:aws:secretsmanager:region:account:secret:migrator \
  bash deploy/aws-vm/bootstrap-database.sh
```

The script retrieves values directly from Secrets Manager, passes passwords to
`psql` through process-scoped environment variables, and streams sealed DSNs
back to Secrets Manager through stdin. It never prints a credential or places a
password in a command-line argument.

The host template defaults to an SSM-managed public-subnet EC2 host with a
public IP but no inbound security-group rule. Set
`ReviewEndpointMode=cloudfront` after the AWS account is permitted to
create CloudFront distributions. CloudFront uses its default TLS certificate
and an origin-verification header; the internet-facing ALB rejects direct
requests. If CloudFront account verification is unavailable, set
`ReviewEndpointMode=apigateway`. That mode uses the API Gateway default HTTPS
endpoint, a VPC link, and an internal ALB. The VM remains unreachable directly
in both modes. For a custom domain, use an ACM certificate and HTTPS listener.

API Gateway mode sets `VIDEO_MAX_UPLOAD_BYTES=8388608`. This leaves headroom
under the HTTP API 10 MB request ceiling for multipart fields. The browser uses
a bounded bitrate for 10–20 second mobile clips and rejects larger files before
upload; the backend enforces the same limit and bounds WebM conversion to 20
seconds. Larger-file production intake must use a separate presigned-S3 and
asynchronous scanning workflow instead of raising this value behind API Gateway.

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
install -o 10001 -g 10001 -m 0400 /secure/input/database-runtime-url \
  /opt/crime-platform/secrets/database-runtime-url
install -o 10001 -g 10001 -m 0400 /secure/input/database-migrator-url \
  /opt/crime-platform/secrets/database-migrator-url
install -o 10001 -g 10001 -m 0400 /secure/input/reka-api-key \
  /opt/crime-platform/secrets/reka-api-key
install -o 10001 -g 10001 -m 0400 /secure/input/twilio-voice.json \
  /opt/crime-platform/secrets/twilio-voice.json
```

For mock demonstrations, materialize the generated Secrets Manager JSON with
`configured` set to `false`; it also contains a generated callback-token key
needed for restart-safe opaque webhook routing. Do not replace it with a
hand-written one-field file. For
an opted-in live demo, update the Secrets Manager value directly (never Git or
chat) with the provider credentials and approved originating-number secret
reference, materialize the protected file, and only then set
`DISPATCH_MODE=live`. Hash each explicitly opted-in E.164 destination with
SHA-256, place only those hashes in `DISPATCH_APPROVED_DESTINATION_SHA256`, and
set `DISPATCH_EXTERNAL_CALLS_ENABLED=true` immediately before the supervised
demo. The provider rechecks this server-side allowlist for every call. Return
the kill switch to `false` afterward. Contact destinations remain separate
`secret://` references and are never returned by the API.

In AWS, retrieve each value from Secrets Manager directly into a protected
temporary file, install it as above, and securely remove the temporary file.
The numeric owner is the fixed `crime` runtime identity in the API image.
Compose mounts `database-migrator-url` only into the one-shot `migrate`
service. API and worker containers receive only `database-runtime-url`; setting
`DATABASE_URL` cannot make the migration CLI fall back to runtime credentials.
The same-host proxy normalizes its upstream Host to `localhost`, which Compose
always adds to the API allowlist, and caps total requests at 9 MiB.
`MAX_REQUEST_BYTES` uses the same bounded multipart envelope while the media
payload itself remains limited to 8 MiB. Camera permission is restricted to
the same HTTPS origin.

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
degraded Reka state. Run the two-tenant database RLS test, the optional direct
role-separation test with protected test-only migrator/runtime DSNs, and a
synthetic MP4 flow before opening the demo endpoint.
