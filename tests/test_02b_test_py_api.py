from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.invoke_integration_tests import FunctionStatus
from tests.conftest import FunctionStatusHandler


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
