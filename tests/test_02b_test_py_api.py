from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

import time
from contextlib import contextmanager

import pytest
from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.invoke_integration_tests import (
    FunctionStatus,
    IntegrationTestWorkflowRunner,
)

WORKFLOW_FILE_PATH = "main.json"
TIMEOUT = 1800
CHECK_INTERVAL = 1


class FunctionStatusHandler:
    def __init__(self, runner: IntegrationTestWorkflowRunner):
        self.runner = runner

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


class WorkflowHandler:
    def __init__(self):
        self.runner = IntegrationTestWorkflowRunner(
            workflow_file_path=WORKFLOW_FILE_PATH,
            timeout=TIMEOUT,
            check_interval=CHECK_INTERVAL,
        )

    @contextmanager
    def function_status_handler(self):
        try:
            self.runner.trigger_workflow()
            yield FunctionStatusHandler(self.runner)
        finally:
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


@pytest.fixture(scope="session", autouse=True)
def workflow_handler():
    return WorkflowHandler()


@pytest.fixture(scope="session", autouse=True)
def function_status_handler(workflow_handler: WorkflowHandler):
    with workflow_handler.function_status_handler() as handler:
        yield handler


@pytest.fixture(scope="session", autouse=True)
def s3_client(workflow_handler: WorkflowHandler):
    return workflow_handler.runner.s3_client


def test_py_api(function_status_handler: FunctionStatusHandler, s3_client: S3Client):
    function_status_handler.wait_for("test_py_api")

    invocation_folder = get_invocation_folder(
        function_status_handler.runner.faasr_payload
    )

    # Check for log files
    key = f"{invocation_folder}/test_py_api.txt"

    assert (
        function_status_handler.runner.get_function_statuses()["test_py_api"]
        == FunctionStatus.COMPLETED
    )
    assert (
        s3_client.head_object(
            Bucket=function_status_handler.runner.bucket_name,
            Key=key,
        )
    ) is not None
