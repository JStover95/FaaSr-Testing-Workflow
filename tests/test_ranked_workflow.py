import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="ranked.json") as tester:
        yield tester


def test_not_ranked(tester: WorkflowTester):
    tester.wait_for("test_not_ranked")
    tester.assert_function_completed("test_not_ranked")
    tester.assert_content_equals("test_not_ranked.txt", "Rank: 1\nMax rank: 1")


def test_red_1(tester: WorkflowTester):
    tester.wait_for("test_ranked(1)")
    tester.assert_function_completed("test_ranked(1)")
    tester.assert_content_equals("test_ranked(1).txt", "Rank: 1\nMax rank: 2")


def test_ranked_2(tester: WorkflowTester):
    tester.wait_for("test_ranked(2)")
    tester.assert_function_completed("test_ranked(2)")
    tester.assert_content_equals("test_ranked(2).txt", "Rank: 2\nMax rank: 2")
