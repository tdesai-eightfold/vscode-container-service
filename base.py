"""
Cloud-agnostic base classes for container instance management.
Switch providers by implementing CloudBaseClass.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import subprocess


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


class ContainerInstanceClient(ABC):
    """
    Common abstract interface for container instance operations.
    Providers implement this (e.g. OracleContainerInstanceClient) and the
    main provider class delegates create/destroy/list/get to it.
    """

    @abstractmethod
    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
    ) -> InstanceInfo:
        """Launch a container instance."""
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None:
        """Delete a container instance by ID."""
        ...

    @abstractmethod
    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        """List container instances."""
        ...

    @abstractmethod
    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        """Get container instance by ID."""
        ...


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
    def get_vpc(self, subnet_id: Optional[str] = None) -> VpcInfo:
        """
        Get VPC info for the given subnet. Does not create VPC/subnet.
        :param subnet_id: Required. Existing subnet ID (from provider config or argument).
        :return: VpcInfo with subnet_id for instance placement.
        :raises ValueError: If subnet_id is not set. Create a VPC/subnet on the provider and set subnet_id in provider.json.
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

    @abstractmethod
    def create_workspace(
        self,
        workspace_hash: str,
        image: str,
        port: int = 8080,
        project_name: str = "codeserver",
    ) -> dict:
        """
        Create workspace: container instance + DNS record.
        :return: dict with workspace_hash, container_id, container_ip, internal_dns, etc.
        """
        ...

    @abstractmethod
    def destroy_workspace(self, workspace_hash: str, project_name: str = "codeserver") -> None:
        """Destroy workspace: delete DNS record, then delete container."""
        ...

    # -------------------------------------------------------------------------
    # Build (buildx) and ensure-built helpers; optional registry check override
    # -------------------------------------------------------------------------

    def image_exists_locally(self, image_ref: str) -> bool:
        """Return True if the image:tag exists locally (docker image inspect)."""
        try:
            subprocess.run(
                ["docker", "image", "inspect", image_ref],
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def image_exists_in_registry(self, image_url: str, tag: str) -> bool:
        """
        Return True if the image with tag exists in the provider's registry.
        Override in providers that support it (e.g. ECR describe_images).
        Default: False (assume not present).
        """
        return False

    def build_image_with_buildx(
        self,
        context_path: str,
        dockerfile_path: str,
        image_tag: str,
        platform: str = "linux/amd64",
        build_args: Optional[dict[str, str]] = None,
        push: bool = False,
    ) -> None:
        """
        Build an image with docker buildx (same logic as aws-push.sh).
        :param context_path: Build context directory (e.g. REPO_ROOT).
        :param dockerfile_path: Path to Dockerfile (e.g. context_path/Dockerfile.base).
        :param image_tag: Full image tag to build (e.g. account.dkr.ecr.region.amazonaws.com/repo:latest).
        :param platform: Target platform (e.g. linux/amd64).
        :param build_args: Optional dict of build-args (e.g. {"MISE_CANDIDATE_TOOLS": "python@3.12"}).
        :param push: If True, add --push to buildx (build and push in one step).
        """
        context_path = Path(context_path).resolve()
        dockerfile_path = Path(dockerfile_path).resolve()
        if not context_path.is_dir():
            raise FileNotFoundError(f"Build context path is not a directory: {context_path}")
        if not dockerfile_path.is_file():
            raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")

        cmd = [
            "docker", "buildx", "build",
            "--platform", platform,
            "-f", str(dockerfile_path),
            "-t", image_tag,
        ]
        if build_args:
            for k, v in build_args.items():
                cmd.extend(["--build-arg", f"{k}={v}"])
        if push:
            cmd.append("--push")
        cmd.append(str(context_path))

        subprocess.run(cmd, check=True)

    def ensure_image_built(
        self,
        image_url: str,
        context_path: str,
        dockerfile_path: str,
        tag: str = "latest",
        platform: str = "linux/amd64",
        build_args: Optional[dict[str, str]] = None,
        push: bool = False,
    ) -> bool:
        """
        Check if image with tag exists (locally, or in registry if push and provider supports it).
        If not, build with buildx (and push in one step if push=True).
        :return: True if image is available (already existed or was built).
        """
        base_no_tag = image_url.rsplit(":", 1)[0] if ":" in image_url else image_url
        full_image = f"{base_no_tag}:{tag}"

        # Prefer local check; if pushing, allow provider to check registry to skip build
        if self.image_exists_locally(full_image):
            if push:
                self.push_image(base_no_tag, full_image, tag=tag)
            return True
        if push and self.image_exists_in_registry(base_no_tag, tag):
            return True

        self.build_image_with_buildx(
            context_path=context_path,
            dockerfile_path=dockerfile_path,
            image_tag=full_image,
            platform=platform,
            build_args=build_args,
            push=push,
        )
        return True

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
