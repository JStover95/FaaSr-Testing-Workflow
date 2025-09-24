import os
import sys
import time

import pytest
from botocore.exceptions import ClientError
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.workflow_runner import (
    FunctionStatus,
    WorkflowRunner,
)

load_dotenv()

TIMEOUT = 120
CHECK_INTERVAL = 1


class WorkflowTester:
    def __init__(self, workflow_file_path: str):
        self.runner = WorkflowRunner(
            workflow_file_path=workflow_file_path,
            timeout=TIMEOUT,
            check_interval=CHECK_INTERVAL,
            stream_logs=True,
        )

    @property
    def bucket_name(self):
        return self.runner.bucket_name

    @property
    def s3_client(self):
        return self.runner.s3_client

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

    def get_s3_key(self, file_name: str):
        return (
            f"integration-tests/{self.runner.faasr_payload['InvocationID']}/{file_name}"
        )

    def wait_for(self, function_name: str):
        status = self.runner.get_function_statuses()[function_name]
        while not (
            status == FunctionStatus.COMPLETED
            or status == FunctionStatus.NOT_INVOKED
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

    def assert_object_exists(self, object_name: str):
        key = self.get_s3_key(object_name)
        assert self.s3_client.head_object(Bucket=self.bucket_name, Key=key) is not None

    def assert_object_does_not_exist(self, object_name: str):
        key = self.get_s3_key(object_name)
        with pytest.raises(ClientError):
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)

    def assert_content_equals(self, object_name: str, expected_content: str):
        key = self.get_s3_key(object_name)
        content = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
        assert content["Body"].read().decode("utf-8") == expected_content

    def assert_function_completed(self, function_name: str):
        assert (
            self.runner.get_function_statuses()[function_name]
            == FunctionStatus.COMPLETED
        )

    def assert_function_not_invoked(self, function_name: str):
        assert (
            self.runner.get_function_statuses()[function_name]
            == FunctionStatus.NOT_INVOKED
        )
