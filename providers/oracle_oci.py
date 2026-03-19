"""
Oracle Cloud Infrastructure (OCI) provider implementation.
"""
import logging
import subprocess
import time
from typing import Optional

from oci.artifacts.artifacts_client import ArtifactsClient
from oci.artifacts.models import CreateContainerRepositoryDetails
from oci.container_instances.container_instance_client import ContainerInstanceClient as OciContainerInstanceClient
from oci.container_instances.models import (
    CreateContainerDetails,
    CreateContainerInstanceDetails,
    CreateContainerInstanceShapeConfigDetails,
    CreateContainerVnicDetails,
)
from oci.core.virtual_network_client import VirtualNetworkClient
from oci.dns.dns_client import DnsClient
from oci.dns.models import PatchDomainRecordsDetails, RecordOperation
from oci.identity.identity_client import IdentityClient
from oci.config import from_file

from ..base import (
    CloudBaseClass,
    Container,
    ContainerInstanceClient,
    InstanceInfo,
    RegistryInfo,
    VpcInfo,
)

logger = logging.getLogger(__name__)


class OracleContainerInstanceClient(ContainerInstanceClient):
    """OCI implementation of ContainerInstanceClient; wraps OCI SDK container instance APIs."""

    def __init__(
        self,
        config: dict,
        region: str,
        compartment_id: str,
        availability_domain: Optional[str] = None,
    ):
        self._config = config
        self._region = region
        self._compartment_id = compartment_id
        self._availability_domain = availability_domain
        self._container_client = OciContainerInstanceClient(self._config)
        self._container_client.base_client.set_region(region)
        self._vnc = VirtualNetworkClient(self._config)
        self._vnc.base_client.set_region(region)
        self._identity_client = IdentityClient(self._config)
        self._identity_client.base_client.set_region(region)

    def _get_private_ip_from_vnic_id(self, vnic_id: str) -> Optional[str]:
        try:
            vnic = self._vnc.get_vnic(vnic_id)
            return vnic.data.private_ip if vnic.data else None
        except Exception:
            return None

    def _get_availability_domain(self) -> str:
        if self._availability_domain:
            return self._availability_domain
        ads = self._identity_client.list_availability_domains(
            compartment_id=self._compartment_id
        )
        if not ads.data:
            raise ValueError("No availability domains found")
        return ads.data[0].name

    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
    ) -> InstanceInfo:
        ad = self._get_availability_domain()
        display_name = f"{project_name}-{instance_name}"
        image_url = registry.image_url(container.image_name, container.tag)
        container_details = CreateContainerDetails(
            display_name=container.display_name or container.name,
            image_url=image_url,
        )
        shape_config = CreateContainerInstanceShapeConfigDetails(
            ocpus=container.cpu,
            memory_in_gbs=container.memory_gb,
        )
        vnic_details = CreateContainerVnicDetails(subnet_id=vpc.subnet_id)
        create_details = CreateContainerInstanceDetails(
            compartment_id=self._compartment_id,
            availability_domain=ad,
            display_name=display_name,
            shape="CI.Standard.E4.Flex",
            shape_config=shape_config,
            containers=[container_details],
            vnics=[vnic_details],
        )
        response = self._container_client.create_container_instance(
            create_container_instance_details=create_details
        )
        work_request_id = response.headers.get("opc-work-request-id")
        instance_id: str
        if work_request_id:
            for _ in range(120):
                wr = self._container_client.get_work_request(work_request_id)
                if wr.data.status in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(5)
            if wr.data.status == "FAILED":
                raise RuntimeError(f"Container instance creation failed: {wr.data}")
            if not wr.data.resources:
                raise RuntimeError("Work request succeeded but no resources returned")
            instance_id = wr.data.resources[0].identifier
        else:
            instance_id = getattr(response.data, "id", str(response))
        instance = self._container_client.get_container_instance(instance_id)
        private_ip = None
        if instance.data.vnics and instance.data.vnics[0].vnic_id:
            private_ip = self._get_private_ip_from_vnic_id(instance.data.vnics[0].vnic_id)
        return InstanceInfo(
            id=instance.data.id,
            name=display_name,
            status=instance.data.lifecycle_state or "ACTIVE",
            private_ip=private_ip,
            url=f"http://{private_ip or '<ip>'}:{container.ports[0]}" if container.ports else None,
            provider="oci",
        )

    def destroy_instance(self, instance_id: str) -> None:
        self._container_client.delete_container_instance(container_instance_id=instance_id)

    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        comp = compartment_or_project or self._compartment_id
        response = self._container_client.list_container_instances(compartment_id=comp)
        items = response.data.items if hasattr(response.data, "items") else (response.data or [])
        result = []
        for inst in items:
            result.append(
                InstanceInfo(
                    id=inst.id,
                    name=inst.display_name or inst.id,
                    status=inst.lifecycle_state or "UNKNOWN",
                    private_ip=None,
                    provider="oci",
                )
            )
        return result

    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        try:
            inst = self._container_client.get_container_instance(instance_id)
            private_ip = None
            if inst.data.vnics and inst.data.vnics[0].vnic_id:
                private_ip = self._get_private_ip_from_vnic_id(inst.data.vnics[0].vnic_id)
            return InstanceInfo(
                id=inst.data.id,
                name=inst.data.display_name or inst.data.id,
                status=inst.data.lifecycle_state or "UNKNOWN",
                private_ip=private_ip,
                provider="oci",
            )
        except Exception:
            return None


class OracleCloudProvider(CloudBaseClass):
    """Oracle OCI implementation of CloudBaseClass."""

    def __init__(
        self,
        compartment_id: str,
        region: str = "ap-hyderabad-1",
        config_profile: str = "DEFAULT",
        config_file: str = "~/.oci/config",
        ocir_namespace: Optional[str] = None,
        subnet_id: Optional[str] = None,
        availability_domain: Optional[str] = None,
        dns_zone_name: Optional[str] = None,
        dns_view_id: Optional[str] = None,
        external_url_template: Optional[str] = None,
    ):
        self._config = from_file(config_file, config_profile)
        self._compartment_id = compartment_id
        self._region = region
        self._subnet_id = subnet_id
        self._ocir_namespace = ocir_namespace or self._config.get("tenancy", "")
        self._availability_domain = availability_domain
        self._dns_zone_name = dns_zone_name or "workspace.internal"
        self._dns_view_id = dns_view_id
        self._external_url_template = external_url_template or "https://tdesai.editor-{workspace_hash}.oracle.com"

        self._artifacts_client = ArtifactsClient(self._config)
        self._artifacts_client.base_client.set_region(region)
        self._container_instance_client = OracleContainerInstanceClient(
            config=self._config,
            region=region,
            compartment_id=self._compartment_id,
            availability_domain=self._availability_domain,
        )
        self._vnc = VirtualNetworkClient(self._config)
        self._vnc.base_client.set_region(region)
        self._identity_client = IdentityClient(self._config)
        self._identity_client.base_client.set_region(region)
        self._dns_client = DnsClient(self._config)
        self._dns_client.base_client.set_region(region)

    @property
    def provider_name(self) -> str:
        return "oci"

    def ensure_registry_repo(self, repo_name: str, is_public: bool = True) -> RegistryInfo:
        """Create or get OCIR repository."""
        try:
            create_details = CreateContainerRepositoryDetails(
                compartment_id=self._compartment_id,
                display_name=repo_name,
                is_public=is_public,
            )
            response = self._artifacts_client.create_container_repository(
                create_container_repository_details=create_details
            )
            repo_id = response.data.id
        except Exception as e:
            if "NAMESPACE_CONFLICT" in str(e) or "already exists" in str(e).lower():
                repos = self._artifacts_client.list_container_repositories(
                    compartment_id=self._compartment_id,
                    display_name=repo_name,
                )
                items = repos.data.items if repos.data else []
                if not items:
                    raise
                repo_id = items[0].id
            else:
                raise
        base_url = f"{self._region}.ocir.io/{self._ocir_namespace}"
        return RegistryInfo(repo_id=repo_id, base_url=base_url, region=self._region)

    def push_image(self, image_url: str, local_image: str, tag: str = "latest") -> None:
        """Push local image via docker/podman."""
        target = image_url.rsplit(":", 1)[0] + f":{tag}" if ":" not in image_url else image_url
        for cmd in ["docker", "podman"]:
            try:
                subprocess.run(
                    [cmd, "tag", local_image, target],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    [cmd, "push", target],
                    check=True,
                    capture_output=True,
                )
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
        raise RuntimeError("Neither docker nor podman found for image push")

    def get_vpc(self, subnet_id: Optional[str] = None) -> VpcInfo:
        """Get VPC info for the given subnet. Does not create; subnet_id is required."""
        sid = subnet_id or self._subnet_id
        if not sid:
            raise ValueError(
                "subnet_id required. Create a VPC and subnet on the provider (OCI Console or CLI) "
                "and set subnet_id in provider.json."
            )
        return VpcInfo(subnet_id=sid, region=self._region)

    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
    ) -> InstanceInfo:
        return self._container_instance_client.create_instance(
            container, instance_name, vpc, registry, project_name
        )

    def destroy_instance(self, instance_id: str) -> None:
        self._container_instance_client.destroy_instance(instance_id)

    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        return self._container_instance_client.list_instances(compartment_or_project)

    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        return self._container_instance_client.get_instance(instance_id)

    def create_dns_record(self, hostname: str, ip: str, ttl: int = 30) -> None:
        """Create A record in OCI private DNS zone workspace.internal."""
        if not self._dns_view_id:
            logger.warning("dns_view_id not configured; skipping DNS record creation")
            return
        try:
            add_op = RecordOperation(
                domain=hostname,
                rtype="A",
                rdata=ip,
                ttl=ttl,
                operation=RecordOperation.OPERATION_ADD,
            )
            details = PatchDomainRecordsDetails(items=[add_op])
            self._dns_client.patch_domain_records(
                zone_name_or_id=self._dns_zone_name,
                domain=hostname,
                patch_domain_records_details=details,
                scope="PRIVATE",
                view_id=self._dns_view_id,
                compartment_id=self._compartment_id,
            )
            logger.info("Created DNS record %s -> %s", hostname, ip)
        except Exception as e:
            logger.exception("OCI DNS create failed: %s", e)
            raise

    def delete_dns_record(self, hostname: str) -> None:
        """Delete A record from OCI private DNS zone."""
        if not self._dns_view_id:
            logger.warning("dns_view_id not configured; skipping DNS record deletion")
            return
        try:
            remove_op = RecordOperation(
                domain=hostname,
                rtype="A",
                operation=RecordOperation.OPERATION_REMOVE,
            )
            details = PatchDomainRecordsDetails(items=[remove_op])
            self._dns_client.patch_domain_records(
                zone_name_or_id=self._dns_zone_name,
                domain=hostname,
                patch_domain_records_details=details,
                scope="PRIVATE",
                view_id=self._dns_view_id,
                compartment_id=self._compartment_id,
            )
            logger.info("Deleted DNS record %s", hostname)
        except Exception as e:
            logger.exception("OCI DNS delete failed: %s", e)
            raise

    def create_workspace(
        self,
        workspace_hash: str,
        image: str,
        port: int = 8080,
        project_name: str = "codeserver",
    ) -> dict:
        """
        Create container + DNS record. Returns workspace info.
        Sequence: create_container -> get IP -> create DNS -> return.
        """
        container_name = f"codeserver-{workspace_hash}"
        # Check for duplicate (create_instance produces display_name = project_name + "-" + instance_name)
        instances = self.list_instances()
        for inst in instances:
            if inst.name and (inst.name == container_name or f"codeserver-{workspace_hash}" in inst.name):
                raise ValueError(f"Container already exists for workspace_hash={workspace_hash}")

        # Parse image: "codercom/code-server:latest" or "region.ocir.io/ns/repo:tag"
        if ":" in image:
            image_name, image_tag = image.rsplit(":", 1)
        else:
            image_name, image_tag = image, "latest"

        # Public Docker Hub image vs OCIR
        if "ocir.io" in image:
            registry = self.ensure_registry_repo(image_name.split("/")[-1] if "/" in image_name else "code-server")
            container = Container(
                name="code-server",
                image_name=image_name.split("/")[-1] if "/" in image_name else image_name,
                tag=image_tag,
                cpu=1.0,
                memory_gb=2.0,
                ports=[port],
            )
        else:
            # Public image (e.g. codercom/code-server:latest)
            base_url = "docker.io" if "/" in image_name else "docker.io/library"
            registry = RegistryInfo(repo_id="", base_url=base_url, region=self._region)
            container = Container(
                name="code-server",
                image_name=image_name,
                tag=image_tag,
                cpu=1.0,
                memory_gb=2.0,
                ports=[port],
            )
        vpc = self.get_vpc(subnet_id=self._subnet_id)
        dns_created = False
        try:
            instance = self.create_instance(
                container, workspace_hash, vpc, registry, project_name=project_name
            )
            private_ip = instance.private_ip
            if not private_ip:
                raise RuntimeError("Container created but no private IP returned")

            internal_dns = f"{workspace_hash}.{self._dns_zone_name}"
            self.create_dns_record(internal_dns, private_ip, ttl=30)
            dns_created = True

            external_url = self._external_url_template.format(workspace_hash=workspace_hash)
            return {
                "workspace_hash": workspace_hash,
                "container_name": container_name,
                "container_id": instance.id,
                "container_ip": private_ip,
                "internal_dns": internal_dns,
                "external_url": external_url,
            }
        except Exception as e:
            if dns_created:
                try:
                    self.delete_dns_record(f"{workspace_hash}.{self._dns_zone_name}")
                except Exception as cleanup_err:
                    logger.exception("DNS cleanup failed: %s", cleanup_err)
            logger.exception("OCI create_workspace failed: %s", e)
            raise

    def destroy_workspace(self, workspace_hash: str, project_name: str = "codeserver") -> None:
        """
        Delete DNS record -> delete container.
        """
        container_name = f"codeserver-{workspace_hash}"

        instances = self.list_instances()
        instance_id = None
        for inst in instances:
            if inst.name and (inst.name == container_name or f"codeserver-{workspace_hash}" in inst.name):
                instance_id = inst.id
                break

        if not instance_id:
            raise ValueError(f"No container found for workspace_hash={workspace_hash}")

        internal_dns = f"{workspace_hash}.{self._dns_zone_name}"
        try:
            self.delete_dns_record(internal_dns)
        except Exception as e:
            logger.exception("DNS delete failed (continuing with container delete): %s", e)

        self.destroy_instance(instance_id)
