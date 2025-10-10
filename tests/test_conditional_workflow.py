import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="conditional.json") as tester:
        yield tester


def test_dont_run_on_true(tester: WorkflowTester):
    tester.wait_for("dont-run-on-true")
    tester.assert_function_not_invoked("dont-run-on-true")


def test_dont_run_on_false(tester: WorkflowTester):
    tester.wait_for("dont-run-on-false")
    tester.assert_function_not_invoked("dont-run-on-false")


def test_run_on_true(tester: WorkflowTester):
    tester.wait_for("run-on-true")
    tester.assert_function_completed("run-on-true")


def test_run_on_false(tester: WorkflowTester):
    tester.wait_for("run-on-false")
    tester.assert_function_completed("run-on-false")
