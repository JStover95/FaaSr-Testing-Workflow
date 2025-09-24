from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def handler():
    with WorkflowTester(workflow_file_path="conditional.json") as handler:
        yield handler


def test_run_on_true(handler: WorkflowTester):
    handler.wait_for("run_on_true")
    handler.assert_function_completed("run_on_true")


def test_run_on_false(handler: WorkflowTester):
    handler.wait_for("run_on_false")
    handler.assert_function_completed("run_on_false")


def test_dont_run_on_true(handler: WorkflowTester):
    handler.wait_for("dont_run_on_true")
    handler.assert_function_not_invoked("dont_run_on_true")


def test_dont_run_on_false(handler: WorkflowTester):
    handler.wait_for("dont_run_on_false")
    handler.assert_function_not_invoked("dont_run_on_false")
