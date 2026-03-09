"""
VSCode Container Manager - cloud-agnostic container instance management.
Switch providers by implementing CloudBaseClass.
"""
from .base import (
    CloudBaseClass,
    Container,
    InstanceInfo,
    RegistryInfo,
    VpcInfo,
)


def get_provider(provider: str, **kwargs) -> CloudBaseClass:
    """Factory: return provider instance by name."""
    if provider.lower() in ("oci", "oracle"):
        from .providers.oracle_oci import OracleCloudProvider  # requires: pip install oci
        return OracleCloudProvider(**kwargs)
    raise ValueError(f"Unknown provider: {provider}. Supported: oci")
