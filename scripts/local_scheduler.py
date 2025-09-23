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
        container_image = self.faasr.get("ActionContainers", {}).get(
            function, "ghcr.io/faasr/github-actions-python:dev"
        )
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
            "My_S3_Bucket_AccessKey": os.getenv("My_S3_Bucket_AccessKey", ""),
            "My_S3_Bucket_SecretKey": os.getenv("My_S3_Bucket_SecretKey", ""),
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
            logger.info(f"Pulling container image: {container_image}")
            self.docker_client.images.pull(container_image)

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


class LocalWorkflowAdapter:
    """
    Adapter class that bridges the gap between workflow files and LocalScheduler.

    This class provides a simple interface to run workflows locally using Docker
    instead of GitHub Actions.
    """

    def __init__(self, workflow_file_path):
        """
        Initialize the local workflow adapter.

        Args:
            workflow_file_path: str -- Path to the workflow JSON file
        """
        self.workflow_file_path = workflow_file_path
        self.workflow_data = self._read_workflow_file()
        self.faasr_payload = None

    def _read_workflow_file(self):
        """Read and parse the workflow JSON file."""
        try:
            with open(self.workflow_file_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Error: Workflow file {self.workflow_file_path} not found")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in workflow file {self.workflow_file_path}")
            sys.exit(1)

    def _get_credentials(self):
        """Get credentials from environment variables."""
        return {
            "My_GitHub_Account_TOKEN": os.getenv("GITHUB_TOKEN"),
            "My_Minio_Bucket_ACCESS_KEY": os.getenv("MINIO_ACCESS_KEY"),
            "My_Minio_Bucket_SECRET_KEY": os.getenv("MINIO_SECRET_KEY"),
        }

    def _create_faasr_payload_from_local_file(self):
        """Create FaaSrPayload from local workflow file."""
        # Get credentials
        credentials = self._get_credentials()

        # Create a copy of workflow data and add credentials
        processed_workflow = self.workflow_data.copy()

        # Add credentials to ComputeServers and DataStores
        for server_name, server_config in processed_workflow.get(
            "ComputeServers", {}
        ).items():
            if server_config.get("FaaSType") == "GitHubActions":
                server_config["Token"] = credentials.get("My_GitHub_Account_TOKEN", "")

        for store_name, store_config in processed_workflow.get(
            "DataStores", {}
        ).items():
            if "Minio" in store_name or "S3" in store_name:
                store_config["AccessKey"] = credentials.get(
                    "My_Minio_Bucket_ACCESS_KEY", ""
                )
                store_config["SecretKey"] = credentials.get(
                    "My_Minio_Bucket_SECRET_KEY", ""
                )

        return FaaSrPayload(processed_workflow)

    def trigger_workflow(self):
        """
        Trigger the workflow using the LocalScheduler.
        """
        # Get the function to invoke
        function_invoke = self.workflow_data.get("FunctionInvoke")
        if not function_invoke:
            print("Error: No FunctionInvoke specified in workflow file")
            sys.exit(1)

        if function_invoke not in self.workflow_data["ActionList"]:
            print(f"Error: FunctionInvoke '{function_invoke}' not found in ActionList")
            sys.exit(1)

        # Get action and server configuration
        action_data = self.workflow_data["ActionList"][function_invoke]
        server_name = action_data["FaaSServer"]
        server_config = self.workflow_data["ComputeServers"][server_name]
        faas_type = server_config["FaaSType"].lower()

        print(f"Using LocalScheduler for '{function_invoke}' on {faas_type}...")

        # Create FaaSrPayload instance
        self.faasr_payload = self._create_faasr_payload_from_local_file()

        # Create LocalScheduler instance
        try:
            scheduler = LocalScheduler(self.faasr_payload)
        except Exception as e:
            print(f"Error creating LocalScheduler: {e}")
            sys.exit(1)

        # Get workflow name for prefixing (if available)
        workflow_name = self.workflow_data.get("WorkflowName", "")

        # Use the LocalScheduler to trigger the function
        try:
            print(f"✓ Using LocalScheduler to trigger function: {function_invoke}")
            scheduler.trigger_func(workflow_name, function_invoke)
            print("✓ Workflow triggered successfully using LocalScheduler!")
        except Exception as e:
            print(f"✗ Error triggering workflow with LocalScheduler: {e}")
            sys.exit(1)


def main():
    """Main entry point for the local scheduler."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run FaaSr workflows locally with Docker"
    )
    parser.add_argument(
        "--workflow-file", required=True, help="Path to workflow JSON file"
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Clean up Docker containers before exit"
    )

    args = parser.parse_args()

    print("=" * 60)
    print("FaaSr Local Scheduler")
    print("Running workflows locally with Docker")
    print("=" * 60)

    # Verify workflow file exists
    if not os.path.exists(args.workflow_file):
        print(f"Error: Workflow file {args.workflow_file} not found")
        sys.exit(1)

    # Create local workflow adapter
    try:
        adapter = LocalWorkflowAdapter(args.workflow_file)
    except Exception as e:
        print(f"Error initializing local workflow adapter: {e}")
        sys.exit(1)

    # Trigger the workflow using local Docker execution
    try:
        adapter.trigger_workflow()
        print("\n" + "=" * 60)
        print("Local execution completed successfully!")
        print("The workflow has been executed using Docker containers.")
        print("=" * 60)
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"\nLocal execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
