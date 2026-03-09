"""
Configuration - edit for your environment.
"""
# Provider: "oci" (Oracle). Add "aws", "gcp" when implemented.
PROVIDER = "oci"

# OCI settings
OCI_COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaa..."
OCI_REGION = "ap-hyderabad-1"
OCI_SUBNET_ID = "ocid1.subnet.oc1..aaaaaaaa..."
OCI_OCIR_NAMESPACE = "axxxxxxxxxx"  # From OCI Console → Profile → Tenancy
OCI_AVAILABILITY_DOMAIN = ""  # Leave empty to use first AD

# Common
PROJECT_NAME = "codeserver"
IMAGE_NAME = "code-server-base"
IMAGE_TAG = "latest-python"
