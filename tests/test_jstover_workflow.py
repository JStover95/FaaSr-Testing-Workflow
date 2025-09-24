from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="jstover.json") as tester:
        yield tester


def test_py_api(tester: WorkflowTester):
    tester.wait_for("test_py_api")

    tester.assert_object_exists("input1.txt")
    tester.assert_object_exists("input2.txt")
    tester.assert_object_exists("input3.txt")
