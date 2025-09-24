from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="conditional.json") as tester:
        yield tester


def test_run_on_true(tester: WorkflowTester):
    tester.wait_for("run_on_true")
    tester.assert_function_completed("run_on_true")


def test_run_on_false(tester: WorkflowTester):
    tester.wait_for("run_on_false")
    tester.assert_function_completed("run_on_false")


def test_dont_run_on_true(tester: WorkflowTester):
    tester.wait_for("dont_run_on_true")
    tester.assert_function_not_invoked("dont_run_on_true")


def test_dont_run_on_false(tester: WorkflowTester):
    tester.wait_for("dont_run_on_false")
    tester.assert_function_not_invoked("dont_run_on_false")
