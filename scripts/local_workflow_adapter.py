#!/usr/bin/env python3

import logging
import os
import sys
import uuid

from invoke_workflow import WorkflowMigrationAdapter
from local_scheduler import LocalScheduler

logger = logging.getLogger(__name__)
logger.formatter = logging.Formatter("[%(levelname)s] [%(filename)s] %(message)s")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


class LocalWorkflowAdapter(WorkflowMigrationAdapter):
    def trigger_workflow(self):
        """
        Trigger the workflow using the Scheduler class.

        This copies the original functionality exactly except that it uses the LocalScheduler class
        instead of the Scheduler class.
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

        print(
            f"Migrating to Scheduler-based invocation for '{function_invoke}' on {faas_type}..."
        )

        # Create FaaSrPayload instance
        self.faasr_payload = self._create_faasr_payload_from_local_file()
        self.faasr_payload["InvocationID"] = str(uuid.uuid4())

        # Create Scheduler instance
        try:
            scheduler = LocalScheduler(self.faasr_payload)
        except Exception as e:
            print(f"Error creating Scheduler: {e}")
            sys.exit(1)

        # Get workflow name for prefixing (if available)
        workflow_name = self.workflow_data.get("WorkflowName", "")

        # Use the Scheduler to trigger the function
        # This replaces all the individual trigger_* methods from invoke_workflow.py
        try:
            print(f"✓ Using Scheduler to trigger function: {function_invoke}")
            scheduler.trigger_func(workflow_name, function_invoke)
            print("✓ Workflow triggered successfully using Scheduler!")
        except Exception as e:
            print(f"✗ Error triggering workflow with Scheduler: {e}")
            sys.exit(1)


def main():
    """Main entry point for the local scheduler."""
    import argparse

    from dotenv import load_dotenv

    load_dotenv()

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
