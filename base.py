"""
Cloud-agnostic base classes for container instance management.
Switch providers by implementing CloudBaseClass.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Container:
    """Common container spec - portable across cloud providers."""

    name: str
    image_name: str  # Repo/image name (e.g. code-server-base)
    tag: str = "latest"  # Image tag (e.g. latest-python)
    display_name: Optional[str] = None
    ports: list[int] = field(default_factory=lambda: [80])
    environment: dict[str, str] = field(default_factory=dict)
    cpu: float = 1.0
    memory_gb: float = 2.0

    def __post_init__(self) -> None:
        if self.display_name is None:
            self.display_name = self.name


@dataclass
class InstanceInfo:
    """Result of create_instance - common across providers."""

    id: str
    name: str
    status: str
    url: Optional[str] = None  # Access URL (e.g. http://ip:80)
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    provider: str = ""


@dataclass
class VpcInfo:
    """VPC/network info - common across providers."""

    subnet_id: str
    vpc_id: Optional[str] = None
    region: Optional[str] = None


@dataclass
class RegistryInfo:
    """Registry/repo info - common across providers."""

    repo_id: str
    base_url: str  # Base URL only, e.g. ap-hyderabad-1.ocir.io/ax2yzgp7isxi
    region: Optional[str] = None

    def image_url(self, image_name: str, tag: str = "latest") -> str:
        """Build full image URL: base_url/image_name:tag."""
        return f"{self.base_url.rstrip('/')}/{image_name}:{tag}"


class CloudBaseClass(ABC):
    """
    Abstract base class for cloud providers.
    Implement these methods to add a new provider (AWS, GCP, Azure, etc.).
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier (e.g. 'oci', 'aws')."""
        ...

    @abstractmethod
    def ensure_registry_repo(self, repo_name: str, is_public: bool = True) -> RegistryInfo:
        """
        Create or get container registry repository.
        Returns RegistryInfo with image_url for push/pull.
        """
        ...

    @abstractmethod
    def push_image(self, image_url: str, local_image: str, tag: str = "latest") -> None:
        """
        Push local image to registry.
        :param image_url: Full registry URL (e.g. region.ocir.io/ns/repo)
        :param local_image: Local image name (e.g. myapp:local)
        :param tag: Tag to push
        """
        ...

    @abstractmethod
    def get_or_create_vpc(self, subnet_id: Optional[str] = None) -> VpcInfo:
        """
        Get or create VPC/subnet for container instances.
        :param subnet_id: If provided, use existing subnet
        :return: VpcInfo with subnet_id for instance placement
        """
        ...

    @abstractmethod
    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
    ) -> InstanceInfo:
        """
        Launch a container instance.
        :param container: Container spec (image_name, tag)
        :param instance_name: Unique name for the instance
        :param vpc: Network placement
        :param registry: Registry with base_url to build image URL
        :param project_name: Project/prefix for display name
        :return: InstanceInfo with id, status, url
        """
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None:
        """Delete a container instance by ID."""
        ...

    @abstractmethod
    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        """
        List running container instances.
        :param compartment_or_project: Provider-specific filter (compartment_id, project, etc.)
        """
        ...

    @abstractmethod
    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        """Get instance details by ID."""
        ...

    def create_dns_record(self, hostname: str, ip: str, ttl: int = 30) -> None:
        """
        Create a DNS A record. Override in providers that support DNS (e.g. OCI).
        Default: no-op for providers without DNS.
        """
        pass

    def delete_dns_record(self, hostname: str) -> None:
        """
        Delete a DNS A record. Override in providers that support DNS.
        Default: no-op for providers without DNS.
        """
        pass
