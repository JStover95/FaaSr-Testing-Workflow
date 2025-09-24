from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def handler():
    with WorkflowTester(workflow_file_path="jstover.json") as handler:
        yield handler


def test_py_api(handler: WorkflowTester):
    handler.wait_for("test_py_api")

    handler.assert_object_exists("input1.txt")
    handler.assert_object_exists("input2.txt")
    handler.assert_object_exists("input3.txt")
