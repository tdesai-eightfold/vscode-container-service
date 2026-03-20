# VSCode Container Manager

Cloud-agnostic container instance management. Switch providers by implementing `CloudBaseClass`.

## Architecture

- **CloudBaseClass** (ABC) – abstract methods every provider must implement
- **Container** – common container spec (name, image_name, tag, cpu, memory, ports)
- **InstanceInfo**, **VpcInfo**, **RegistryInfo** – common result types

## Providers

| Provider | Class | Status |
|----------|-------|--------|
| Oracle OCI | `OracleCloudProvider` | Implemented |
| AWS (ECR + ECS Fargate + Route 53) | `AWSECRProvider` | Implemented |

## Usage

### OCI (Python)

```python
# Run from v2-ubuntu-base-container/ or add to PYTHONPATH
from vscode_container_manager import get_provider, Container
from vscode_container_manager.base import VpcInfo

provider = get_provider("oci",
    compartment_id="ocid1.compartment...",
    region="<your-region>",
    subnet_id="ocid1.subnet...",
    ocir_namespace="<your-ocir-namespace>",
)

registry = provider.ensure_registry_repo("code-server-base")
# registry.base_url = "<region>.ocir.io/<namespace>"

container = Container(
    name="code-server",
    image_name="code-server-base",
    tag="latest-python",
    cpu=1.0,
    memory_gb=2.0,
    ports=[80],
)

vpc = provider.get_vpc(subnet_id="ocid1.subnet...")
instance = provider.create_instance(container, "user1", vpc, registry, project_name="codeserver")
print(instance.id, instance.url)

provider.push_image(registry.image_url("code-server-base", "latest-python"), "my-local-image:dev", "latest-python")
provider.destroy_instance(instance.id)
```

### AWS (Python)

```python
from vscode_container_manager import get_provider

provider = get_provider(
    "aws",
    region="<your-region>",
    account_id="<12-digit-account-id>",
    subnet_id="subnet-xxxxxxxx",
    cluster="<ecs-cluster-name>",
    task_definition="<task-family>:<revision>",
    security_groups=["sg-xxxxxxxx"],
    execution_role_arn="arn:aws:iam::<account-id>:role/<execution-role>",
    private_hosted_zone_id="Zxxxxxxxxxxxxxx",
    dns_zone_name="<private-zone-name-without-suffix>",
)
# For workspaces, use REST /workspace/create with a full ECR image URI.
```

## Adding a New Provider

1. Create `providers/<name>.py`
2. Subclass `CloudBaseClass` and implement all abstract methods
3. Register in `get_provider()` in `__init__.py`

## API (FastAPI)

Provider config from `provider.json`; requests only need provider name.

**Setup:** Copy `provider.json.example` to `provider.json` and fill in your own values (never commit secrets).

**OCI example (structure only):**
```json
{
  "oci": {
    "compartment_id": "ocid1...",
    "region": "ap-example-1",
    "subnet_id": "ocid1.subnet...",
    "ocir_namespace": "<namespace>",
    "availability_domain": "",
    "dns_zone_name": "workspace.internal",
    "dns_view_id": "ocid1.dnsview...",
    "external_url_template": "https://<your-gateway>/{workspace_hash}"
  }
}
```

**AWS example (structure only):**
```json
{
  "aws": {
    "region": "ap-example-1",
    "account_id": "<12-digit-account-id>",
    "profile_name": "",
    "subnet_id": "subnet-xxxxxxxx",
    "cluster": "<cluster-name>",
    "task_definition": "<family>:<revision>",
    "security_groups": ["sg-xxxxxxxx"],
    "execution_role_arn": "arn:aws:iam::<account-id>:role/<role-name>",
    "task_role_arn": "",
    "private_hosted_zone_id": "Zxxxxxxxxxxxxxx",
    "dns_zone_name": "<route53-private-zone-name>",
    "aws_access_key_id": "",
    "aws_secret_access_key": "",
    "aws_session_token": "",
    "s3_access_grants": {
      "enabled": true,
      "bucket": "<bucket-name>",
      "prefix": "<s3-prefix>",
      "credential_duration_seconds": 3600
    }
  }
}
```

Use IAM keys in `provider.json` only if required; prefer instance roles, profiles, or environment variables. Omit empty optional fields as needed.

```bash
cd v2-ubuntu-base-container && python -m vscode_container_manager.api
PORT=8000 python -m vscode_container_manager.api
SSL_CERTFILE=/path/to/cert.pem SSL_KEYFILE=/path/to/key.pem python -m vscode_container_manager.api
```

These match `vscode_container_manager/api.py` only (no other HTTP routes).

| Endpoint | Method | Request | Description |
|----------|--------|---------|-------------|
| `/` | GET | — | Service name and links (`/docs`, `/health`, `/providers`) |
| `/providers` | GET | — | `{ "providers": ["oci", "aws", ...] }` from `provider.json` |
| `/destroy` | POST | JSON | `{"instance_id": "...", "provider": "<name>"}` — destroy instance by provider ID |
| `/instances` | POST | JSON | `{"provider": "<name>"}` — list instances |
| `/instances/{instance_id}` | GET | Query `provider` (required) | Get one instance |
| `/workspace/create` | POST | JSON | `{"provider", "workspace_hash", "image", "port"}` — returns `job_id`; poll `/status/{job_id}` |
| `/workspace/destroy` | POST | JSON | `{"provider", "workspace_hash"}` |
| `/status/{job_id}` | GET | — | `creating` \| `completed` (with `result`) \| `failed` (with `error`) |
| `/health` | GET | — | `{ "status": "ok", "service": "..." }` |

Use **POST `/workspace/create`** to provision workspaces (there is no separate `/create` route in this API).

**Example list:**
```bash
curl -X POST https://localhost:443/instances -k -H "Content-Type: application/json" -d '{"provider": "oci"}'
curl -X POST https://localhost:443/instances -k -H "Content-Type: application/json" -d '{"provider": "aws"}'
```

For AWS, `instances[].name` matches `workspace_hash` (ECS `startedBy`).

**Example get instance:**
```bash
curl -k "https://localhost:443/instances/<instance_id>?provider=oci"
curl -k "https://localhost:443/instances/<task-arn-or-id>?provider=aws"
```

**Example providers:**
```bash
curl -k https://localhost:443/providers
```

**Example destroy instance (by id):**
```bash
curl -X POST https://localhost:443/destroy -k -H "Content-Type: application/json" -d '{
  "instance_id": "ocid1.computecontainerinstance...",
  "provider": "oci"
}'
```

### Workspace API

#### OCI (private DNS)

**Create workspace:**
```bash
curl -X POST https://localhost:443/workspace/create -k -H "Content-Type: application/json" -d '{
  "provider": "oci",
  "workspace_hash": "a92f13",
  "image": "codercom/code-server:latest",
  "port": 80
}'
```

Poll status (replace `<job_id>` from the create response):
```bash
curl -k https://localhost:443/status/<job_id>
```

Example `GET /status/{job_id}` when `status` is `completed` (payload is in `result`):
```json
{
  "status": "completed",
  "result": {
    "workspace_hash": "a92f13",
    "container_name": "codeserver-a92f13",
    "container_ip": "10.0.0.0",
    "internal_dns": "a92f13.workspace.internal",
    "external_url": "https://<your-gateway>/a92f13"
  }
}
```
(Exact keys in `result` depend on OCI vs AWS provider.)

**Destroy workspace:**
```bash
curl -X POST https://localhost:443/workspace/destroy -k -H "Content-Type: application/json" -d '{
  "provider": "oci",
  "workspace_hash": "a92f13"
}'
```

Requires `dns_zone_name`, `dns_view_id`, and optional `external_url_template` in `provider.json`.

---

#### AWS (ECS Fargate + Route 53 private zone)

Injects `CONTAINER_HASH` env = `workspace_hash`. Creates A record `{workspace_hash}.{dns_zone_name}` → task ENI private IP.

Optional **S3 Access Grants**: scoped credentials for `s3://<bucket>/<prefix>/hash-<workspace_hash>/` may be passed to the task (see `provider.json`).

**Create workspace:**
```bash
curl -X POST https://localhost:443/workspace/create -k -H "Content-Type: application/json" -d '{
  "provider": "aws",
  "workspace_hash": "myworksp01",
  "image": "<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>",
  "port": 80
}'
```

`image` must be a full URI your task definition can pull. Poll `GET /status/{job_id}`.

**Destroy workspace:**
```bash
curl -X POST https://localhost:443/workspace/destroy -k -H "Content-Type: application/json" -d '{
  "provider": "aws",
  "workspace_hash": "myworksp01"
}'
```

**AWS minimum:** `region`, `account_id`, `subnet_id`, `cluster`, `task_definition`, `execution_role_arn`, `private_hosted_zone_id`, `dns_zone_name`. IAM must allow ECS task lifecycle and Route 53 changes in that zone.

---

### provider.json (workspace DNS) — fields only

**OCI:** `dns_zone_name`, `dns_view_id`, `external_url_template`

**AWS:** `dns_zone_name`, `private_hosted_zone_id`

## Prerequisites

- **OCI:** `~/.oci/config`, `pip install oci`
- **AWS:** `pip install boto3`, AWS credentials (recommended: IAM role / profile), ECS + Fargate + Route 53 private hosted zone

## Running in production (systemd)

Use valid Python package layout (`python -m vscode_container_manager.api` from `v2-ubuntu-base-container`). For port 443 + TLS, set `SSL_CERTFILE` / `SSL_KEYFILE` and ensure the service user can read the key; or use `CAP_NET_BIND_SERVICE` / reverse proxy.

## Troubleshooting

- **provider.json not found** — Run from `v2-ubuntu-base-container` or set paths so `provider.json` is found next to the package.

- **Provider init / 500** — OCI: check config and OCIDs. AWS: cluster, task definition, subnet, execution role, IAM permissions.

- **Workspace create (OCI)** — Missing `dns_zone_name` / `dns_view_id`.

- **Workspace create (AWS)** — Task definition, networking, ECR pull, zone ID vs zone name; check ECS stopped-task reason.

- **Relative import error** — Use `python -m vscode_container_manager.api`, not `python api.py` when imports expect a package.

- **TLS Permission denied** — Service user must read the private key file.
