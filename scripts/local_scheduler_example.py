#!/usr/bin/env python3
"""
Example usage of the LocalScheduler for running FaaSr workflows locally with Docker.

This script demonstrates how to use the LocalScheduler to run workflows locally
instead of using GitHub Actions.
"""

import os

from local_scheduler import LocalWorkflowAdapter


def main():
    """Example usage of LocalWorkflowAdapter."""

    # Example workflow file path
    workflow_file = "main.json"  # or any other workflow JSON file

    print("FaaSr Local Scheduler Example")
    print("=" * 40)

    # Check if Docker is available
    try:
        import docker

        docker.from_env().ping()
        print("✓ Docker is available")
    except Exception as e:
        print(f"✗ Docker not available: {e}")
        print("Please ensure Docker is installed and running")
        return

    # Check if workflow file exists
    if not os.path.exists(workflow_file):
        print(f"✗ Workflow file {workflow_file} not found")
        print("Please ensure you have a valid workflow JSON file")
        return

    # Set up environment variables (these would normally come from your environment)
    # For this example, we'll set some dummy values
    os.environ.setdefault("GITHUB_TOKEN", "your-github-token")
    os.environ.setdefault("MINIO_ACCESS_KEY", "your-minio-access-key")
    os.environ.setdefault("MINIO_SECRET_KEY", "your-minio-secret-key")
    os.environ.setdefault("My_S3_Bucket_AccessKey", "your-s3-access-key")
    os.environ.setdefault("My_S3_Bucket_SecretKey", "your-s3-secret-key")

    try:
        # Create the local workflow adapter
        print(f"Loading workflow from: {workflow_file}")
        adapter = LocalWorkflowAdapter(workflow_file)

        # Trigger the workflow using local Docker execution
        print("Starting local workflow execution...")
        adapter.trigger_workflow()

        print("✓ Workflow execution completed successfully!")

    except Exception as e:
        print(f"✗ Error during workflow execution: {e}")
        return


if __name__ == "__main__":
    main()
