import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="jstover.json") as tester:
        yield tester


def test_py_api(tester: WorkflowTester):
    tester.wait_for("create_input")

    tester.assert_object_exists("input1.txt")
    tester.assert_object_does_not_exist("does_not_exist.txt")
    tester.assert_content_equals("input2.txt", "Test input2")
