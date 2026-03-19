"""
VSCode Container Manager - cloud-agnostic container instance management.
Switch providers by implementing CloudBaseClass.
"""
from .base import (
    CloudBaseClass,
    Container,
    ContainerInstanceClient,
    InstanceInfo,
    RegistryInfo,
    VpcInfo,
)


def get_provider(provider: str, **kwargs) -> CloudBaseClass:
    """Factory: return provider instance by name."""
    p = provider.lower()
    if p in ("oci", "oracle"):
        from .providers.oracle_oci import OracleCloudProvider  # requires: pip install oci
        return OracleCloudProvider(**kwargs)
    if p == "aws":
        from .providers.aws_ecr import AWSECRProvider  # requires: pip install boto3
        return AWSECRProvider(**kwargs)
    raise ValueError(f"Unknown provider: {provider}. Supported: oci, aws")
