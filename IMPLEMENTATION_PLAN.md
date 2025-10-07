# Testing Framework Implementation Plan

This document outlines a possible implementation of the FaaSr Integration Testing Framework into the existing FaaSr-Backend package.

## Key Assumptions & Questions

The workflow runner makes a few key assumptions:

### Workflow Isolation

A workflow invocation is isolated from other workflow runs on S3 using its `InvocationID`. For example:

```plaintext
s3-bucket/
└── folder-name/
    └── 4f290d29-7826-412c-a148-2d8f66c81b2f/
        ├── function1_output.txt
        └── function2_output.txt
    └── ba7e2b99-ebe2-41b2-8c0d-56cbaea34993/
        ├── function1_output.txt
        └── function2_output.txt
```

Currently this is achieved by:

- Explicitly setting the `InvocationID` and `InvocationTimestamp` in the workflow runner before triggering the workflow.
- Using the invocation ID from within the function to isolate each run:

```python
import json
import os


def get_invocation_id() -> str:
    try:
        overwritten = json.loads(os.environ["OVERWRITTEN"])
        if (
            not isinstance(overwritten, dict)
            or "InvocationID" not in overwritten
            or not isinstance(overwritten["InvocationID"], str)
            or overwritten["InvocationID"].strip() == ""
        ):
            raise EnvironmentError("InvocationID is not set")
        return overwritten["InvocationID"]
    except KeyError as e:
        raise EnvironmentError("OVERWRITTEN is not set") from e
    except json.JSONDecodeError as e:
        raise EnvironmentError("OVERWRITTEN is not valid JSON") from e
    except EnvironmentError:
        raise


def create_input(folder: str, input1: str) -> None:
    invocation_id = get_invocation_id()

    # Create input1 (input to be deleted using test_py_api)
    with open(input1, "w") as f:
        ...

    faasr_put_file(
        local_file=input1,
        remote_file=f"{invocation_id}/{input1}",
        remote_folder=folder,
    )
```

This is not a good design for external end users because requiring them to get an invocation ID for each function adds unnecessary complexity. Instead we could:

1. Simple solution: remove workflow isolation from the `WorkflowRunner` entirely, or make it optional
2. Complicated solution: Add workflow isolation directly to FaaSr-Backend

### Access to Workflow Data and `FaaSrPayload` Instance

`WorkflowRunner` currently uses the `workflow_data` and `faasr_payload` attributes of the deprecated `WorkflowMigrationAdapter` for the following:

- Getting the invocation folder with `get_invocation_folder`
- Setting `InvocationID` and `InvocationTimestamp`
- Building adjacency graphs
- Getting the workflow name, invocation, action list, and default data store config

To maintain this behavior, the invoker called by the FAASR-INVOKE action could do one of the following:

1. Separate workflow data and invocation into different functions outside of `main`, allowing `WorkflowRunner` to use its own invocation logic.
2. Write the invoker as a class (like `WorkflowMigrationAdapter`) that the `WorkflowRunner` could then override or use.

## Pip Optional Dependencies

The testing framework can come bundled with [optional dependencies](https://pydevtools.com/handbook/explanation/what-are-optional-dependencies-and-dependency-groups/) for testing. These optional dependencies could then be installed with `pip install faasr-backend[testing]`.

Optional dependencies can be defined with `setup.py` or `pyproject.toml` (recommended).

### 1. Defining Optional Dependencies with `setup.py`

```python
from setuptools import find_packages, setup

with open("requirements.txt") as f:
    requirements = f.read().splitlines()

setup(
    name="FaaSr_py",
    version="0.1.13",
    packages=find_packages(),
    include_package_data=True,
    install_requires=requirements,
    extras_require={
        "testing": [
            "pytest>=7.4.0",
        ]
    }
)
```

### 2. Defining Optional Dependencies with `pyproject.toml`

```toml
[project]
name = "FaaSr_py"
version = "1.0.0"
dependencies = ["..."]

[project.optional-dependencies]
testing = ["pytest>=7.4.0"]
```

## Implementation with Existing Package Code

It may be ideal to use the `testing` directory for the testing utils and move actual tests out of the package directory.

```plaintext
FaaSr_py/
└── testing/
    ├── __init__.py
    ├── utils/
    │   ├── __init__.py
    │   ├── exceptions.py
    │   ├── enums.py
    │   ├── utils.py
    ├── function_logger.py
    ├── function_monitor.py
    ├── workflow_runner.py
    └── workflow_tester.py
```

### Files and Interfaces

#### `exceptions.py`

Includes custom exceptions:

- `InitializationError`
- `StopMonitoring`

#### `enums.py`

Includes enums for the testing framework:

- `FunctionStatus`
- `InvocationStatus`

#### `utils.py`

Includes miscellaneous utility functions for the testing framework.

- Status flags
  - `pending`
  - `invoked`
  - `not_invoked`
  - `running`
  - `completed`
  - `failed`
  - `skipped`
  - `timed_out`
  - `has_run`
  - `has_completed`
  - `has_final_state`
- `extract_function_name`
- `get_s3_path`

#### `function_logger.py`

Includes the `FunctionLogger` class. This pulls the logs of a single function from S3.

**Properties:**

- `logs: list[str]`
- `logs_content: str`
- `logs_complete: bool`
- `logs_key: str`

**Methods:**

- `start() -> None`

#### `function_monitor.py`

Includes the `FunctionMonitor` class. This monitors S3 for the status of a single function.

**Properties:**

- `function_complete: bool`
- `function_failed: bool`
- `invocations: set[str] | None`
- `done_key: str`

**Methods:**

- `get_invocation_status(function_name: str) -> InvocationStatus`
- `start() -> None`

#### `workflow_runner.py`

Includes the `WorkflowRunner` class. This runs a workflow and monitors the statuses of all functions.

**Properties:**

- `monitoring_complete: bool`
- `shutdown_requested: bool`

**Methods:**

- `get_function_statuses() -> dict[str, FunctionStatus]`
- `shutdown(timeout: float | None = None) -> bool`
- `force_shutdown() -> None`
- `cleanup() -> None`
- `trigger_workflow() -> None`

#### `workflow_tester.py`

Includes the `WorkflowTester` class. This includes utilities for running tests against a workflow based only on S3 outputs.

**Properties:**

- `bucket_name: str`
- `s3_client: S3Client`

**Methods:**

- `get_s3_key(file_name: str) -> str`
- `wait_for(function_name) -> str`
- `assert_object_exists(object_name: str) -> None`
- `assert_object_does_not_exist(object_name: str) -> None`
- `assert_content_equals(object_name: str, expected_content: str) -> None`
- `assert_function_completed(function_name: str) -> None`
- `assert_function_not_invoked(function_name: str) -> None`

## Example Usage

### Pytest Integration

The simplest use case will be for end users to run integration tests against their workflows. A user may have a repository with their workflow, functions, and tests:

```plaintext
user-repo/
├── functions/                  # User-defined functions
│   ├── __init__.py
│   ├── initialize.py
│   ├── process.py
│   └── validate.py
├── workflow.json               # User-defined workflow
└── tests/                      # User-defined test suite with pytest
    ├── __init__.py
    └── integration_tests.py
```

The user will then import `WorkflowTester` to run tests against their workflow:

```python
import pytest

from FaaSr_py.testing import WorkflowTester


@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester(workflow_file_path="workflow.json") as tester:
        yield tester


def test_initialize(tester: WorkflowTester):
    tester.wait_for("initialize")
    ...


def test_process(tester: WorkflowTester):
    tester.wait_for("process")
    ...


def test_validate(tester: WorkflowTester):
    tester.wait_for("validate")
    ...
```

### Using `WorkflowRunner Directly

A user may want to use the `WorkflowRunner` directly for use cases outside of pytest. For example:

```python
runner = WorkflowRunner(
    workflow_file_path="workflow.json",
    timeout=120,
    check_interval=1,
    stream_logs=True,
)

# Start the workflow
runner.trigger_workflow()

# Monitor status changes
previous_statuses = {}
while not runner.monitoring_complete:
    current_statuses = runner.get_function_statuses()
    for function_name, status in current_statuses.items():
        if (
            function_name not in previous_statuses
            or previous_statuses[function_name] != status
        ):
            match status:
                case FunctionStatus.PENDING:
                    ...
                case FunctionStatus.INVOKED:
                    ...
                case FunctionStatus.NOT_INVOKED:
                    ...
                case FunctionStatus.RUNNING:
                    ...
                case FunctionStatus.COMPLETED:
                    ...
                case FunctionStatus.FAILED:
                    ...
                case FunctionStatus.SKIPPED:
                    ...
                case FunctionStatus.TIMEOUT:
                    ...

    previous_statuses = current_statuses.copy()
```

## Features to Add

- Performance monitoring
