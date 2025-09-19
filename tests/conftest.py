import os
import sys
import time

import pytest
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.workflow_runner import (
    FunctionStatus,
    WorkflowRunner,
)

load_dotenv()

WORKFLOW_FILE_PATH = "main.json"
TIMEOUT = 1800
CHECK_INTERVAL = 1


class WorkflowHandler:
    def __init__(self):
        self.runner = WorkflowRunner(
            workflow_file_path=WORKFLOW_FILE_PATH,
            timeout=TIMEOUT,
            check_interval=CHECK_INTERVAL,
            stream_logs=True,
        )

    @property
    def bucket_name(self):
        return self.runner.bucket_name

    def get_s3_key(self, file_name: str):
        return (
            f"integration-tests/{self.runner.faasr_payload['InvocationID']}/{file_name}"
        )

    def __enter__(self):
        self.runner.trigger_workflow()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup()

    def _cleanup(self):
        """
        Cleanup resources when exiting the context manager.
        This ensures proper thread cleanup even if an exception occurs.
        """
        try:
            # Attempt graceful shutdown first
            if not self.runner.shutdown(timeout=10):
                # If graceful shutdown fails, force shutdown
                self.runner.force_shutdown()

            # Perform comprehensive cleanup
            self.runner.cleanup()

        except Exception as e:
            # Log cleanup errors but don't raise them to avoid masking original exceptions
            print(f"Warning: Error during cleanup: {e}")

        # Return False to not suppress any exceptions that occurred in the context
        return False

    def wait_for(self, function_name: str):
        status = self.runner.get_function_statuses()[function_name]
        while not (
            status == FunctionStatus.COMPLETED
            or status == FunctionStatus.FAILED
            or status == FunctionStatus.SKIPPED
            or status == FunctionStatus.TIMEOUT
        ):
            time.sleep(CHECK_INTERVAL)
            status = self.runner.get_function_statuses()[function_name]

        if status == FunctionStatus.FAILED:
            raise Exception(f"Function {function_name} failed")
        elif status == FunctionStatus.SKIPPED:
            raise Exception(f"Function {function_name} skipped")
        elif status == FunctionStatus.TIMEOUT:
            raise Exception(f"Function {function_name} timed out")

        return status


@pytest.fixture(scope="session", autouse=True)
def handler():
    with WorkflowHandler() as handler:
        yield handler


@pytest.fixture(scope="session", autouse=True)
def s3_client(handler: WorkflowHandler):
    return handler.runner.s3_client
