from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.invoke_integration_tests import FunctionStatus
from tests.conftest import WorkflowHandler


def test_py_api(handler: WorkflowHandler, s3_client: S3Client):
    handler.wait_for("test_py_api")

    invocation_folder = get_invocation_folder(handler.runner.faasr_payload)

    # Check for log files
    key = f"{invocation_folder}/test_py_api.txt"

    assert (
        handler.runner.get_function_statuses()["test_py_api"]
        == FunctionStatus.COMPLETED
    )
    assert (
        s3_client.head_object(
            Bucket=handler.runner.bucket_name,
            Key=key,
        )
    ) is not None
