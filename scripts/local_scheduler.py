#!/usr/bin/env python3

import json
import logging
import os
import sys
import time

import docker
from FaaSr_py.engine.faasr_payload import FaaSrPayload
from FaaSr_py.engine.scheduler import Scheduler

logger = logging.getLogger(__name__)
logger.formatter = logging.Formatter("[%(levelname)s] [%(filename)s] %(message)s")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


class LocalScheduler(Scheduler):
    """
    Local Scheduler that overrides GitHub Actions invocation to run workflows locally with Docker.

    This class extends the standard Scheduler to replace GitHub Actions API calls with local
    Docker container execution, maintaining the same environment and behavior.
    """

    def __init__(self, faasr: FaaSrPayload):
        """
        Initialize the LocalScheduler.

        Args:
            faasr: FaaSrPayload instance containing workflow configuration
            docker_client: Optional Docker client instance (creates new one if None)
        """
        super().__init__(faasr)

        try:
            self.docker_client = docker.from_env()
            # Test Docker connection
            self.docker_client.ping()
            logger.info("Docker client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            sys.exit(1)

    def invoke_gh(self, next_compute_server, function, workflow_name=None):
        """
        Override GitHub Actions invocation to run locally with Docker.

        This method replaces the GitHub API call with local Docker container execution,
        maintaining the same environment variables and behavior as GitHub Actions.

        Args:
            next_compute_server: dict -- next compute server configuration
            function: str -- name of the function to invoke
            workflow_name: str -- optional workflow name for prefixing
        """
        if workflow_name:
            function = f"{workflow_name}-{function}"
            logger.debug(f"Prepending workflow name. Full function: {function}")

        # Get container image from ActionContainers or use default
        # container_image = self.faasr.get("ActionContainers", {}).get(
        #     function, "ghcr.io/faasr/github-actions-python:dev"
        # )
        container_image = "faasr-github-actions-local:latest"
        logger.info(f"Using container image: {container_image}")

        # Prepare environment variables (same as GitHub Actions)
        env_vars = self._prepare_environment_variables(next_compute_server)

        # Run Docker container locally
        try:
            self._run_docker_container(container_image, env_vars, function)
            logger.info(f"✓ Successfully executed function: {function}")
        except Exception as e:
            logger.error(f"✗ Failed to execute function {function}: {e}")
            sys.exit(1)

    def _prepare_environment_variables(self, next_compute_server):
        """
        Prepare environment variables for Docker container execution.

        Args:
            next_compute_server: dict -- compute server configuration

        Returns:
            dict -- environment variables for Docker container
        """
        # Get overwritten fields (same logic as original invoke_gh)
        overwritten_fields = self.faasr.overwritten.copy()

        # Handle UseSecretStore logic
        if next_compute_server.get("UseSecretStore"):
            if "ComputeServers" in overwritten_fields:
                del overwritten_fields["ComputeServers"]
            if "DataStores" in overwritten_fields:
                del overwritten_fields["DataStores"]
        else:
            overwritten_fields["ComputeServers"] = self.faasr["ComputeServers"]
            overwritten_fields["DataStores"] = self.faasr["DataStores"]

        # Prepare environment variables
        env_vars = {
            "TOKEN": next_compute_server.get("Token", ""),
            "My_GitHub_Account_PAT": next_compute_server.get("Token", ""),
            "My_S3_Bucket_AccessKey": os.getenv("MY_S3_BUCKET_ACCESSKEY", ""),
            "My_S3_Bucket_SecretKey": os.getenv("MY_S3_BUCKET_SECRETKEY", ""),
            "OVERWRITTEN": json.dumps(overwritten_fields),
            "PAYLOAD_URL": self.faasr.url,
        }

        # Add any additional environment variables from the compute server config
        for key, value in next_compute_server.items():
            if key not in [
                "Token",
                "FaaSType",
                "UserName",
                "ActionRepoName",
                "Branch",
                "UseSecretStore",
            ]:
                env_vars[key] = str(value)

        return env_vars

    def _run_docker_container(self, container_image, env_vars, function_name):
        """
        Run the Docker container locally with the same environment as GitHub Actions.

        Args:
            container_image: str -- Docker image to run
            env_vars: dict -- environment variables for the container
            function_name: str -- name of the function being executed
        """
        container_name = f"faasr-{function_name}-{int(time.time())}"

        try:
            # Pull the container image if not already present
            # logger.info(f"Pulling container image: {container_image}")
            # self.docker_client.images.pull(container_image)

            # Run the container
            logger.info(f"Starting container: {container_name}")
            container = self.docker_client.containers.run(
                image=container_image,
                environment=env_vars,
                command=["python3", "faasr_entry.py"],
                working_dir="/action",
                detach=True,
                name=container_name,
                remove=False,  # Keep container for debugging
                stdout=True,
                stderr=True,
                volumes={
                    "/var/run/docker.sock": {
                        "bind": "/var/run/docker.sock",
                        "mode": "rw",
                    }
                },
            )

            # Stream logs in real-time
            logger.info(f"Streaming logs for function: {function_name}")
            for line in container.logs(stream=True, follow=True):
                log_line = line.decode().strip()
                if log_line:  # Only print non-empty lines
                    print(f"[{function_name}] {log_line}")

            # Wait for container completion
            result = container.wait()

            # Check exit status
            if result["StatusCode"] != 0:
                raise Exception(
                    f"Container execution failed with exit code {result['StatusCode']}"
                )

            logger.info(f"✓ Container {container_name} completed successfully")

        except docker.errors.ImageNotFound:
            logger.error(f"Container image not found: {container_image}")
            raise
        except docker.errors.ContainerError as e:
            logger.error(f"Container execution error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error running container: {e}")
            raise
        finally:
            # Clean up container
            try:
                container = self.docker_client.containers.get(container_name)
                container.remove(force=True)
                logger.debug(f"Cleaned up container: {container_name}")
            except Exception:
                pass  # Container might already be removed

    def cleanup_containers(self, pattern="faasr-"):
        """
        Clean up any remaining containers with the given pattern.

        Args:
            pattern: str -- pattern to match container names for cleanup
        """
        try:
            containers = self.docker_client.containers.list(
                all=True, filters={"name": pattern}
            )
            for container in containers:
                if container.name.startswith(pattern):
                    container.remove(force=True)
                    logger.info(f"Cleaned up container: {container.name}")
        except Exception as e:
            logger.warning(f"Error during container cleanup: {e}")
