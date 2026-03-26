"""
AWS (ECR + ECS) provider implementation.
Supports: init, credentials, ensure_registry_repo, push_image, get_vpc (subnet from config only), ECS Fargate lifecycle.
"""
import base64
import hashlib
import json
import logging
import subprocess
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from ..base import (
    CloudBaseClass,
    Container,
    ContainerInstanceClient,
    InstanceInfo,
    RegistryInfo,
    VpcInfo,
)

logger = logging.getLogger(__name__)


def _task_to_instance_info(t: dict, container: Optional[Container] = None) -> InstanceInfo:
    """Map ECS task dict to InstanceInfo. t = task from describe_tasks."""
    tid = t.get("taskArn", "").split("/")[-1]
    last = t.get("lastStatus", "UNKNOWN")
    priv = None
    for att in t.get("attachments", []) or []:
        for d in att.get("details", []) or []:
            if d.get("name") == "privateIPv4Address":
                priv = d.get("value")
                break
    ports = (container.ports if container else [80]) or [80]
    return InstanceInfo(
        id=t.get("taskArn", tid),
        name=t.get("startedBy") or tid,
        status=last,
        private_ip=priv,
        url=f"http://{priv or '<ip>'}:{ports[0]}" if priv else None,
        provider="aws",
    )


class AWSContainerInstanceClient(ContainerInstanceClient):
    """Minimal ECS Fargate implementation of ContainerInstanceClient."""

    def __init__(
        self,
        ecs,
        cluster: str,
        task_definition: str,
        security_groups: list[str],
        ecr=None,
        execution_role_arn: Optional[str] = None,
        task_role_arn: Optional[str] = None,
    ):
        self._ecs = ecs
        self._cluster = cluster
        self._task_def = task_definition
        self._sgs = security_groups or []
        self._ecr = ecr
        self._execution_role_arn = execution_role_arn
        self._task_role_arn = task_role_arn

    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
        task_definition_arn: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> InstanceInfo:
        task_def = task_definition_arn or self._task_def
        env = [{"name": "CONTAINER_HASH", "value": instance_name}]
        if extra_env:
            env.extend([{"name": k, "value": str(v)} for k, v in extra_env.items()])
        r = self._ecs.run_task(
            cluster=self._cluster,
            taskDefinition=task_def,
            launchType="FARGATE",
            startedBy=instance_name,
            overrides={
                "containerOverrides": [
                    {
                        "name": container.name,
                        "environment": env,
                    }
                ]
            },
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [vpc.subnet_id],
                    "securityGroups": self._sgs,
                    "assignPublicIp": "DISABLED",
                }
            },
        )
        tasks = r.get("tasks") or []
        if not tasks:
            raise RuntimeError(r.get("failures", [{}])[0].get("reason", "RunTask failed"))
        task_arn = tasks[0]["taskArn"]
        # Wait for network attachment and get private IP
        for _ in range(24):
            out = self._ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])
            t = (out.get("tasks") or [{}])[0]
            if t.get("lastStatus") in ("RUNNING", "PENDING"):
                info = _task_to_instance_info(t, container)
                if info.private_ip:
                    return info
            __import__("time").sleep(5)
        return _task_to_instance_info(self._ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])["tasks"][0], container)

    def destroy_instance(self, instance_id: str) -> None:
        self._ecs.stop_task(cluster=self._cluster, task=instance_id)

    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        arns = self._ecs.list_tasks(cluster=self._cluster, desiredStatus="RUNNING").get("taskArns") or []
        if not arns:
            return []
        out = self._ecs.describe_tasks(cluster=self._cluster, tasks=arns)
        return [_task_to_instance_info(t) for t in out.get("tasks") or []]

    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        out = self._ecs.describe_tasks(cluster=self._cluster, tasks=[instance_id])
        tasks = out.get("tasks") or []
        return _task_to_instance_info(tasks[0]) if tasks else None

    def ensure_task_definition(
        self,
        container: Container,
        registry: RegistryInfo,
        execution_role_arn: Optional[str] = None,
        family_prefix: str = "codeserver",
    ) -> str:
        """Create task definition from container+registry only (no VPC/infra). Same input -> same hash; reuse existing revision or register new. Uses execution_role_arn from config or argument."""
        role_arn = execution_role_arn or self._execution_role_arn
        if not role_arn:
            raise ValueError("execution_role_arn required: set in provider.json (execution_role_arn) or pass to ensure_task_definition")
        image_uri = registry.image_url(container.image_name, container.tag)
        cpu = max(256, min(4096, int(container.cpu * 1024)))
        memory = max(512, min(30720, int(container.memory_gb * 1024)))
        canonical = {
            "image": image_uri,
            "cpu": cpu,
            "memory": memory,
            "name": container.name,
            "env": sorted((container.environment or {}).items()),
            "ports": sorted((container.ports or [80])),
            "task_role_arn": self._task_role_arn or "",
        }
        if self._ecr:
            try:
                r = self._ecr.describe_images(repositoryName=container.image_name, imageIds=[{"imageTag": container.tag}])
                details = (r.get("imageDetails") or [])
                if details and details[0].get("imagePushedAt"):
                    canonical["image_pushed_at"] = details[0]["imagePushedAt"].isoformat()
            except ClientError:
                pass
        h = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()[:12]
        family = f"{family_prefix}-{h}"
        try:
            r = self._ecs.describe_task_definition(taskDefinition=family)
            return r["taskDefinition"]["taskDefinitionArn"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ClientException", "InvalidParameterException"):
                raise
        env = [{"name": k, "value": str(v)} for k, v in (container.environment or {}).items()]
        ports = container.ports or [80]
        register_kwargs: dict = {
            "family": family,
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": str(cpu),
            "memory": str(memory),
            "executionRoleArn": role_arn,
            "containerDefinitions": [{
                "name": container.name,
                "image": image_uri,
                "essential": True,
                "portMappings": [{"containerPort": p, "protocol": "tcp"} for p in ports],
                "environment": env,
            }],
        }
        if self._task_role_arn:
            register_kwargs["taskRoleArn"] = self._task_role_arn
        r = self._ecs.register_task_definition(**register_kwargs)
        return r["taskDefinition"]["taskDefinitionArn"]


class AWSECRProvider(CloudBaseClass):
    """AWS ECR + optional ECS Fargate container instance lifecycle."""

    def __init__(
        self,
        region: str = "ap-northeast-1",
        account_id: Optional[str] = None,
        profile_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        subnet_id: Optional[str] = None,
        cluster: Optional[str] = None,
        task_definition: Optional[str] = None,
        security_group_ids: Optional[list[str]] = None,
        private_hosted_zone_id: Optional[str] = None,
        dns_zone_name: Optional[str] = None,
        execution_role_arn: Optional[str] = None,
        task_role_arn: Optional[str] = None,
        s3_access_grants: Optional[dict] = None,
    ):
        """
        Initialize AWS provider with credentials.

        Credentials (in order of precedence):
        - Explicit: aws_access_key_id, aws_secret_access_key (, aws_session_token)
        - Profile: profile_name (uses ~/.aws/credentials and ~/.aws/config)
        - Default: boto3 default chain (env AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, or default profile)
        """
        self._region = region
        self._profile_name = profile_name
        self._account_id = account_id

        session_kwargs: dict = {"region_name": region}
        if profile_name:
            session_kwargs["profile_name"] = profile_name
        if aws_access_key_id and aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = aws_access_key_id
            session_kwargs["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                session_kwargs["aws_session_token"] = aws_session_token

        self._session = boto3.Session(**session_kwargs)
        self._ecr = self._session.client("ecr")
        self._s3_access_grants = s3_access_grants or {}
        self._subnet_id = subnet_id
        self._private_hosted_zone_id = private_hosted_zone_id
        self._dns_zone_name = (dns_zone_name or "workspace.internal").rstrip(".")
        self._route53 = self._session.client("route53") if private_hosted_zone_id else None
        self._container_instance_client: Optional[AWSContainerInstanceClient] = None
        if cluster and task_definition:
            self._container_instance_client = AWSContainerInstanceClient(
                self._session.client("ecs"),
                cluster,
                task_definition,
                security_group_ids or [],
                ecr=self._ecr,
                execution_role_arn=execution_role_arn,
                task_role_arn=task_role_arn,
            )

        if not self._account_id:
            try:
                sts = self._session.client("sts")
                identity = sts.get_caller_identity()
                self._account_id = identity["Account"]
            except ClientError as e:
                logger.warning("Could not resolve account_id from STS: %s", e)

    @property
    def provider_name(self) -> str:
        return "aws"

    @property
    def account_id(self) -> Optional[str]:
        """Resolved or configured AWS account ID (used for ECR registry URL)."""
        return self._account_id

    def ensure_registry_repo(self, repo_name: str, is_public: bool = True) -> RegistryInfo:
        """Create or get ECR repository. Returns RegistryInfo with base_url for push/pull."""
        if not self._account_id:
            raise ValueError("account_id required for ECR; set in provider config or ensure credentials are valid")
        try:
            self._ecr.create_repository(
                repositoryName=repo_name,
                imageScanningConfiguration={"scanOnPush": False},
                imageTagMutability="MUTABLE",
            )
            logger.info("Created ECR repository: %s", repo_name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
                raise
            logger.debug("ECR repository already exists: %s", repo_name)

        base_url = f"{self._account_id}.dkr.ecr.{self._region}.amazonaws.com"
        # ECR repo "id" is typically account_id/repo_name for reference
        repo_id = f"{self._account_id}/{repo_name}"
        return RegistryInfo(repo_id=repo_id, base_url=base_url, region=self._region)

    def _ecr_docker_login(self) -> None:
        """Log Docker/Podman into ECR so subsequent push (e.g. buildx --push) succeeds."""
        try:
            token = self._ecr.get_authorization_token()
        except ClientError as e:
            raise RuntimeError(f"ECR get_authorization_token failed: {e}") from e
        auth_data = token["authorizationData"]
        if not auth_data:
            raise RuntimeError("ECR returned no authorization data")
        decoded = base64.b64decode(auth_data[0]["authorizationToken"]).decode()
        user, password = decoded.split(":", 1)
        registry = auth_data[0]["proxyEndpoint"].replace("https://", "").replace("http://", "")
        for cmd in ["docker", "podman"]:
            try:
                subprocess.run(
                    [cmd, "login", "-u", user, "--password-stdin", registry],
                    input=password.encode(),
                    check=True,
                    capture_output=True,
                )
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
        raise RuntimeError("Neither docker nor podman found for ECR login")

    def image_exists_in_registry(self, image_url: str, tag: str) -> bool:
        """Return True if the image tag exists in ECR (describe_images)."""
        parts = image_url.split("/", 1)
        if len(parts) != 2:
            return False
        repo_name = parts[1]
        try:
            self._ecr.describe_images(
                repositoryName=repo_name,
                imageIds=[{"imageTag": tag}],
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ImageNotFoundException":
                return False
            raise

    def push_image(
        self,
        image_url: str,
        local_image: str,
        tag: str = "latest",
        *,
        context_path: Optional[str] = None,
        dockerfile_path: Optional[str] = None,
        build_args: Optional[dict[str, str]] = None,
        platform: str = "linux/amd64",
        **kwargs: Any,
    ) -> None:
        """
        Push local image to ECR. If image is not present locally and context_path/dockerfile_path
        are provided, build with buildx then push.
        """
        base_no_tag = image_url.rsplit(":", 1)[0] if ":" in image_url else image_url
        target = f"{base_no_tag}:{tag}"

        # Check local first; if missing and build params given, build and push
        if not self.image_exists_locally(local_image):
            if context_path and dockerfile_path:
                self._ecr_docker_login()
                self.ensure_image_built(
                    image_url=base_no_tag,
                    context_path=context_path,
                    dockerfile_path=dockerfile_path,
                    tag=tag,
                    platform=platform,
                    build_args=build_args,
                    push=True,
                )
                return
            raise RuntimeError(
                f"Image not found locally: {local_image}. "
                "Provide context_path and dockerfile_path to build and push, or build the image first."
            )

        self._ecr_docker_login()
        for cmd in ["docker", "podman"]:
            try:
                subprocess.run([cmd, "tag", local_image, target], check=True, capture_output=True)
                subprocess.run([cmd, "push", target], check=True, capture_output=True)
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
        raise RuntimeError("Neither docker nor podman found for image push")

    def get_vpc(self, subnet_id: Optional[str] = None) -> VpcInfo:
        """
        Build placement info from configured subnet_id only (no EC2 DescribeSubnets call).
        ECS RunTask validates the subnet when the task is scheduled.
        """
        sid = subnet_id or self._subnet_id
        if not sid:
            raise ValueError(
                "subnet_id required. Create a VPC and subnet on the provider (AWS Console or CLI) "
                "and set subnet_id in provider.json."
            )
        return VpcInfo(subnet_id=sid, vpc_id=None, region=self._region)

    def create_instance(
        self,
        container: Container,
        instance_name: str,
        vpc: VpcInfo,
        registry: RegistryInfo,
        project_name: str = "codeserver",
        extra_env: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> InstanceInfo:
        if not self._container_instance_client:
            raise NotImplementedError("Set cluster and task_definition in provider config for create_instance")
        return self._container_instance_client.create_instance(
            container,
            instance_name,
            vpc,
            registry,
            project_name=project_name,
            extra_env=extra_env,
            **kwargs,
        )

    def destroy_instance(self, instance_id: str) -> None:
        if not self._container_instance_client:
            raise NotImplementedError("Set cluster and task_definition in provider config for destroy_instance")
        self._container_instance_client.destroy_instance(instance_id)

    def list_instances(self, compartment_or_project: Optional[str] = None) -> list[InstanceInfo]:
        if not self._container_instance_client:
            return []
        return self._container_instance_client.list_instances(compartment_or_project)

    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        if not self._container_instance_client:
            return None
        return self._container_instance_client.get_instance(instance_id)

    def _dns_fqdn(self, hostname: str) -> str:
        """Return FQDN with trailing dot for Route 53."""
        name = hostname.rstrip(".")
        if self._dns_zone_name and not name.endswith("." + self._dns_zone_name):
            name = f"{name}.{self._dns_zone_name}" if name else self._dns_zone_name
        return name if name.endswith(".") else name + "."

    def create_dns_record(self, hostname: str, ip: str, ttl: int = 30) -> None:
        """Create A record in Route 53 private hosted zone (same pattern as OCI)."""
        if not self._private_hosted_zone_id or not self._route53:
            logger.warning("private_hosted_zone_id not configured; skipping DNS record creation")
            return
        try:
            fqdn = self._dns_fqdn(hostname)
            self._route53.change_resource_record_sets(
                HostedZoneId=self._private_hosted_zone_id,
                ChangeBatch={
                    "Changes": [{
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": fqdn,
                            "Type": "A",
                            "TTL": ttl,
                            "ResourceRecords": [{"Value": ip}],
                        },
                    }]
                },
            )
            logger.info("Created DNS record %s -> %s", fqdn, ip)
        except ClientError as e:
            logger.exception("AWS Route 53 DNS create failed: %s", e)
            raise

    def delete_dns_record(self, hostname: str) -> None:
        """Delete A record from Route 53 private hosted zone."""
        if not self._private_hosted_zone_id or not self._route53:
            logger.warning("private_hosted_zone_id not configured; skipping DNS record deletion")
            return
        try:
            fqdn = self._dns_fqdn(hostname)
            # Route 53 DELETE requires the full record; list to get current TTL and value
            r = self._route53.list_resource_record_sets(
                HostedZoneId=self._private_hosted_zone_id,
                StartRecordName=fqdn,
                StartRecordType="A",
                MaxItems="1",
            )
            sets = r.get("ResourceRecordSets") or []
            target = next((s for s in sets if s.get("Name") == fqdn and s.get("Type") == "A"), None)
            if not target:
                logger.debug("No A record found for %s; nothing to delete", fqdn)
                return
            self._route53.change_resource_record_sets(
                HostedZoneId=self._private_hosted_zone_id,
                ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": target}]},
            )
            logger.info("Deleted DNS record %s", fqdn)
        except ClientError as e:
            logger.exception("AWS Route 53 DNS delete failed: %s", e)
            raise

    def _get_s3_access_grants_credentials(self, workspace_hash: str) -> Optional[dict[str, str]]:
        """
        Get temporary S3 Access Grants credentials scoped to hash-{workspace_hash}/*.
        Returns dict with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, or None if disabled.
        """
        cfg = self._s3_access_grants
        if not cfg.get("enabled"):
            return None
        bucket = cfg.get("bucket")
        prefix = (cfg.get("prefix") or "candidate-code").rstrip("/")
        duration = int(cfg.get("credential_duration_seconds") or 10800)  # 3 hours default
        account_id = self._account_id
        if not bucket or not account_id:
            return None
        target = f"s3://{bucket}/{prefix}/hash-{workspace_hash}/*"
        try:
            s3control = self._session.client("s3control")
            resp = s3control.get_data_access(
                AccountId=account_id,
                Target=target,
                Permission="READWRITE",
                Privilege="Minimal",
                DurationSeconds=min(max(duration, 900), 43200),
            )
            creds = resp.get("Credentials") or {}
            return {
                "AWS_ACCESS_KEY_ID": creds.get("AccessKeyId", ""),
                "AWS_SECRET_ACCESS_KEY": creds.get("SecretAccessKey", ""),
                "AWS_SESSION_TOKEN": creds.get("SessionToken", ""),
            }
        except ClientError as e:
            logger.exception("S3 Access Grants GetDataAccess failed for %s: %s", target, e)
            return None

    def create_workspace(
        self,
        workspace_hash: str,
        image: str,
        port: int = 8080,
        project_name: str = "codeserver",
    ) -> dict:
        """Create workspace: container + DNS (same pattern as OCI). Uses workspace_hash as instance name and DNS label."""
        if not self._container_instance_client:
            raise NotImplementedError("Set cluster and task_definition in provider config for create_workspace")
        if ":" in image:
            image_name, image_tag = image.rsplit(":", 1)
        else:
            image_name, image_tag = image, "latest"
        if "amazonaws.com" in image_name or "dkr.ecr" in image:
            repo_name = image_name.split("/", 1)[1] if "/" in image_name else image_name
            registry = self.ensure_registry_repo(repo_name, is_public=False)
            container = Container(
                name="code-server",
                image_name=repo_name,
                tag=image_tag,
                cpu=1.0,
                memory_gb=2.0,
                ports=[port],
            )
        else:
            registry = RegistryInfo(repo_id="", base_url="docker.io", region=self._region)
            container = Container(
                name="code-server",
                image_name=image_name,
                tag=image_tag,
                cpu=1.0,
                memory_gb=2.0,
                ports=[port],
            )
        vpc = self.get_vpc()
        task_def_arn = self._container_instance_client.ensure_task_definition(container, registry)

        extra_env: dict[str, str] = {}
        if self._s3_access_grants.get("enabled"):
            creds = self._get_s3_access_grants_credentials(workspace_hash)
            if creds:
                extra_env.update(creds)
                extra_env["S3_WORKSPACE_BUCKET"] = self._s3_access_grants.get("bucket", "")
                extra_env["S3_WORKSPACE_PREFIX"] = self._s3_access_grants.get("prefix", "candidate-code")

        instance = self.create_instance(
            container,
            workspace_hash,
            vpc,
            registry,
            project_name=project_name,
            task_definition_arn=task_def_arn,
            extra_env=extra_env or None,
        )
        private_ip = instance.private_ip or ""
        if not private_ip:
            raise RuntimeError("Container created but no private IP returned")
        internal_dns = f"{workspace_hash}.{self._dns_zone_name}"
        self.create_dns_record(internal_dns, private_ip, ttl=60)
        return {
            "workspace_hash": workspace_hash,
            "container_name": f"codeserver-{workspace_hash}",
            "container_id": instance.id,
            "container_ip": private_ip,
            "internal_dns": internal_dns,
        }

    def destroy_workspace(self, workspace_hash: str, project_name: str = "codeserver") -> None:
        """Destroy workspace: delete DNS, then stop ECS task (find by startedBy=workspace_hash)."""
        instances = self.list_instances()
        instance_id = next((i.id for i in instances if (i.name or "") == workspace_hash), None)
        if not instance_id:
            raise ValueError(f"No container found for workspace_hash={workspace_hash}")
        internal_dns = f"{workspace_hash}.{self._dns_zone_name}"
        try:
            self.delete_dns_record(internal_dns)
        except Exception as e:
            logger.exception("DNS delete failed (continuing): %s", e)
        self.destroy_instance(instance_id)
