from enum import Enum


class CreateInput(Enum):
    INPUT_1_CONTENT = "Test input1"
    INPUT_2_CONTENT = "Test input2"
    INPUT_3_CONTENT = (
        "id,fruit,price\n"
        "1,apple,1.99\n"
        "2,banana,0.16\n"
        "3,strawberry,3.77\n"
    )
    INPUT_4_CONTENT = "Test input4"


class TestPyApi(Enum):
    OUTPUT_1_CONTENT = "Test output1"
    OUTPUT_2_CONTENT = "Test output2"
    
class TestConditional(Enum):
    RUN_TRUE_CONTENT = "test_run_true invoked"
    RUN_FALSE_CONTENT = "test_run_false invoked"
    
TestRank = "Test input for rank "