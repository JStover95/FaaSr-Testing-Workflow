import pytest

from tests.conftest import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="laura.json") as tester:
        yield tester
        
def create_input(tester: WorkflowTester):
    tester.wait_for("create_input")
    tester.assert_function_completed("create_input")
    
    # tester.assert_object_exists("input1.txt")
    # tester.assert_object_does_not_exist("does_not_exist.txt")
    # tester.assert_content_equals("input2.txt", "Test input2")
    
def test_py_api(tester:WorkflowTester):
    tester.wait_for("create_input")
    tester.assert_function_completed("create_input")
    
def test_r_api(tester:WorkflowTester):
    tester.wait_for("test_py_api")
    tester.assert_function_completed("test_py_api")
    
def sync1(tester:WorkflowTester):
    tester.wait_for("sync1")
    tester.assert_function_completed("sync1")
    
def test_run_true(tester:WorkflowTester):
    tester.wait_for("test_run_true")
    tester.assert_function_completed("test_run_true")
    
def test_dontrun_false(tester:WorkflowTester):
    tester.wait_for("test_dontrun_false")
    tester.assert_function_not_invoked("test_dontrun_false")
    
def test_run_false(tester:WorkflowTester):
    tester.wait_for("test_run_false")
    tester.assert_function_completed("test_run_false")
    
def test_dontrun_true(tester:WorkflowTester):
    tester.wait_for("test_dontrun_true")
    tester.assert_function_not_invoked("test_dontrun_true")
    
def test_rank_1(tester:WorkflowTester):
    tester.wait_for("test_rank(1)")
    tester.assert_function_completed("test_rank(1)")
    
def test_rank_2(tester:WorkflowTester):
    tester.wait_for("test_rank(2)")
    tester.assert_function_completed("test_rank(2)")
    
def test_rank_3(tester:WorkflowTester):
    tester.wait_for("test_rank(3)")
    tester.assert_function_completed("test_rank(3)")
    
def test_rank_4(tester:WorkflowTester):
    tester.wait_for("test_rank(4)")
    tester.assert_function_completed("test_rank(4)")
    
def test_rank_5(tester:WorkflowTester):
    tester.wait_for("test_rank(5)")
    tester.assert_function_completed("test_rank(5)")
    
def sync2(tester:WorkflowTester):
    tester.wait_for("sync2")
    tester.assert_function_completed("sync2")

        
