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

## Usage

```python
# Run from v2-ubuntu-base-container/ or add to PYTHONPATH
from vscode_container_manager import get_provider, Container
from vscode_container_manager.base import VpcInfo

# Get OCI provider
provider = get_provider("oci",
    compartment_id="ocid1.compartment...",
    region="ap-hyderabad-1",
    subnet_id="ocid1.subnet...",
    ocir_namespace="ax2yzgp7isxi",
)

# Ensure registry (returns base URL only, same pattern as Terraform)
registry = provider.ensure_registry_repo("code-server-base")
# registry.base_url = "ap-hyderabad-1.ocir.io/ax2yzgp7isxi"

# Create container spec - image_name + tag (create_instance builds full image_url)
container = Container(
    name="code-server",
    image_name="code-server-base",
    tag="latest-python",  # Must match tag used when pushing (see terraform.tfvars)
    cpu=1.0,
    memory_gb=2.0,
    ports=[80],
)

# Get VPC (subnet)
vpc = provider.get_vpc(subnet_id="ocid1.subnet...")

# Create instance (builds image_url from registry.base_url + image_name + tag)
instance = provider.create_instance(container, "user1", vpc, registry, project_name="codeserver")
print(instance.id, instance.url)

# Push image: registry.image_url(image_name, tag) for full URL
provider.push_image(registry.image_url("code-server-base", "latest-python"), "my-local-image:dev", "latest-python")

# Destroy
provider.destroy_instance(instance.id)
```

## Adding a New Provider

1. Create `providers/aws.py` (or `gcp.py`)
2. Subclass `CloudBaseClass` and implement all abstract methods
3. Register in `get_provider()` in `__init__.py`

## API (FastAPI)

Provider config from `provider.json`; requests only need provider name.

**Setup:** Copy `provider.json.example` to `provider.json` and fill in your values:
```json
{
  "oci": {
    "compartment_id": "ocid1.tenancy...",
    "region": "ap-hyderabad-1",
    "subnet_id": "ocid1.subnet...",
    "ocir_namespace": "ax2yzgp7isxi",
    "availability_domain": ""
  }
}
```

```bash
# Run API server
cd v2-ubuntu-base-container && python -m vscode_container_manager.api
# or: uvicorn vscode_container_manager.api:app --reload --port 8000
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/providers` | GET | List configured providers |
| `/create` | POST | Create instance (provider + instance params only) |
| `/destroy` | POST | Destroy instance (instance_id, provider) |
| `/workspace/create` | POST | Create workspace (container + DNS record) |
| `/workspace/destroy` | POST | Destroy workspace (DNS + container) |
| `/instances` | POST | List instances (provider only) |
| `/instances/{instance_id}` | GET | Get instance (?provider=oci) |
| `/health` | GET | Health check |

**Example create:**
```bash
curl -X POST http://localhost:8000/create -H "Content-Type: application/json" -d '{
  "provider": "oci",
  "instance_name": "user1",
  "image_name": "code-server-base",
  "image_tag": "latest-python"
}'
```

**Example list:**
```bash
curl -X POST http://localhost:8000/instances -H "Content-Type: application/json" -d '{"provider": "oci"}'
```

**Example destroy:**
```bash
curl -X POST http://localhost:8000/destroy -H "Content-Type: application/json" -d '{
  "instance_id": "ocid1.computecontainerinstance...",
  "provider": "oci"
}'
```

### Workspace API (container + OCI private DNS)

Creates container with DNS record `{workspace_hash}.workspace.internal` → private IP.

**Create workspace:**
```bash
curl -X POST http://localhost:8000/workspace/create -H "Content-Type: application/json" -d '{
  "provider": "oci",
  "workspace_hash": "a92f13",
  "image": "codercom/code-server:latest",
  "port": 80
}'
```

Use `port: 80` to match the gateway (or align gateway `nginx.conf` with your container port).

Response:
```json
{
  "workspace_hash": "a92f13",
  "container_name": "codeserver-a92f13",
  "container_ip": "10.0.1.12",
  "internal_dns": "a92f13.workspace.internal",
  "external_url": "http://68.233.115.227/a92f13"
}
```

The gateway routes `http://<gateway-ip>/<workspace_hash>` → `http://<hash>.workspace.internal:80`.

**Destroy workspace:** (deletes DNS record, then container)
```bash
curl -X POST http://localhost:8000/workspace/destroy -H "Content-Type: application/json" -d '{
  "provider": "oci",
  "workspace_hash": "a92f13"
}'
```

**provider.json** for workspace/DNS support:
```json
{
  "oci": {
    ...
    "dns_zone_name": "workspace.internal",
    "dns_view_id": "ocid1.dnsview.oc1..aaaaaaaa...",
    "external_url_template": "http://<gateway-public-ip>/{workspace_hash}"
  }
}
```

Replace `<gateway-public-ip>` with your nginx gateway's public IP (e.g. `68.233.115.227`).

## Prerequisites

- OCI: `~/.oci/config` configured, `pip install oci`

## Troubleshooting

- **API won't start / "provider.json not found"**  
  Run from the repo root so the package can find `provider.json`:  
  `cd v2-ubuntu-base-container && python -m vscode_container_manager.api`  
  Or set `PYTHONPATH` and ensure `vscode_container_manager/provider.json` exists (copy from `provider.json.example`).

- **"Provider init failed" / 500 on create or list**  
  OCI provider needs `~/.oci/config` with a valid profile (e.g. `oci cli setup`). Ensure `compartment_id`, `region`, `subnet_id` in `provider.json` match your tenancy.

- **Workspace create fails**  
  For `/workspace/create`, `provider.json` must include `dns_zone_name` and `dns_view_id` (OCI private DNS view). Without them, DNS record creation fails.
