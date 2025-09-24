# FaaSr-Testing-Workflow

This repo includes workflows for FaaSr integration testing:

**Workflows:**

- **`main.json`**: The complete integration testing workflow.
- **`conditional.json`**: A minimal workflow for testing conditional invocation.

## Getting Started

1. If you are using VS Code, make a copy of `.vscode/settings.template.json` and save it as `.vscode/settings.json`

2. Set up the Python virtual environment. Python 3.13 is recommended.

   ```bash
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Make a copy of `.env.template` and save it as `.env`
   - Save your GitHub PAT as `GITHUB_TOKEN`.
   - Save the name of your workflow file as `TEST_WORKFLOW_FILE`.

4. Checkout to a new branch with a recognizable name.

5. Make a copy of `main.json` or another workflow and give it a recognizable name
   - Change the `WorkflowName` attribute to a unique name.
      - **Note**: The workflow name can only contain alphanumeric characters ([a-z], [A-Z], [0-9]) or underscores (_)

6. Commit and push your changes.

7. Register the workflow (via GitHub Actions or from the command line).

   ```bash
   ./register_workflow.sh --workflow-file <Your Workflow File>
   ```

8. Invoke the workflow with the workflow runner.

   ```bash
   python -m scripts.workflow_runner --workflow-file <Your Workflow File>
   ```

## Workflow Runner

The `workflow_runner.py` script enables the execution and monitoring of FaaSr workflows with real-time status tracking and logging capabilities.

### Features

- **Real-time Monitoring**: Continuously monitors workflow execution status
- **Function Status Tracking**: Tracks individual function states (pending, invoked, not invoked, running, completed, failed, skipped, timed out)
- **Log Streaming**: Optional real-time log streaming from S3
- **Graceful Shutdown**: Handles interruption signals (SIGINT, SIGTERM) gracefully
- **Thread-safe Operations**: Safe for concurrent access and monitoring
- **Timeout Management**: Configurable timeouts to prevent hanging workflows
- **S3 Integration**: Monitors function completion through S3 object detection

### Usage

#### Command Line Interface

```bash
python -m scripts.workflow_runner --workflow-file <workflow.json> [options]
```

**Required Arguments:**

- `--workflow-file`: Path to the FaaSr workflow JSON file

**Optional Arguments:**

- `--timeout`: Function timeout in seconds (default: 120). If no function status change is detected within this interval, the workflow runner exits.
- `--check-interval`: Status check interval in seconds (default: 1)
- `--stream-logs`: Enable real-time log streaming to the terminal (default: True)

#### Example Usage

```bash
# Basic usage with default settings
python -m scripts.workflow_runner --workflow-file main.json

# Custom timeout and check interval
python -m scripts.workflow_runner --workflow-file main.json --timeout 300 --check-interval 2

# Disable log streaming
python -m scripts.workflow_runner --workflow-file main.json --stream-logs False
```

### Programmatic Usage

The `WorkflowRunner` class can be used programmatically for more control:

```python
from scripts.workflow_runner import WorkflowRunner

# Initialize the runner
runner = WorkflowRunner(
    workflow_file_path="main.json",
    timeout=300,
    check_interval=2,
    stream_logs=True
)

# Start the workflow
runner.trigger_workflow()

# Monitor status changes
while not runner.is_monitoring_complete():
    statuses = runner.get_function_statuses()
    # Process status updates
    time.sleep(1)

# Cleanup
runner.cleanup()
```

### Function Status States

The workflow runner tracks the following function states:

- **`PENDING`**: Waiting to start
- **`INVOKED`**: Invoked by any function
- **`NOT_INVOKED`**: Not invoked by any function
- **`RUNNING`**: Currently executing
- **`COMPLETED`**: Finished successfully
- **`FAILED`**: Encountered an error
- **`SKIPPED`**: Skipped due to upstream failure
- **`TIMEOUT`**: Was in a non-complete state when the workflow timed out.

### Environment Variables

The following environment variables are required:

- `MY_S3_BUCKET_ACCESSKEY`: S3 access key for monitoring
- `MY_S3_BUCKET_SECRETKEY`: S3 secret key for monitoring
- `GITHUB_TOKEN`: GitHub personal access token
- `GITHUB_REPOSITORY`: GitHub repository name

### Thread Safety

The Workflow Runner is designed to be thread-safe:

- All status updates and logs are protected by locks
- Safe for concurrent access from multiple threads
- Graceful shutdown handling prevents race conditions
- Clean resource management and cleanup

## Testing

### Workflow Tester

A `WorkflowTester` can be used to automatically invoke the workflow when running tests:

- **`tester.bucket_name`**: Get the name of the workflow's data store bucket.
- **`tester.s3_client`**: An S3 client for performing S3 actions.
- **`tester.get_s3_key`**: Get the full S3 key for a given file.
- **`tester.wait_for`**: Wait for the function to complete or have a "not invoked" state. An exception is thrown when a function fails, causing the test to fail automatically.

The `WorkflowTester` also includes helper functions for test assertions:

- **`tester.assert_object_exists`**: Assert whether an object exists in the workflow's data store.
- **`tester.assert_object_does_not_exist`**: Assert whether an object does not exist in the workflow's data store.
- **`tester.assert_content_equals`**: Assert whether an object's content equals a given string.
- **`tester.assert_function_completed`**: Assert whether a function successfully completed.
- **`tester.assert_function_not_invoked`**: Assert whether a function was not invoked (i.e., in a conditional workflow).

### Writing Tests

It is recommended to create a `tester` fixture at the top of each test file that runs the workflow being tested:

```python
@pytest.fixture(scope="module", autouse=True)
def tester():
    with WorkflowTester("conditional.json") as tester:
        yield tester
```

Test data store outputs:

```py
def test_py_api(handler: WorkflowHandler, s3_client: S3Client):
    handler.wait_for("test_py_api")

    # Test that input1 does not exist
    handler.assert_object_does_not_exist("input1.txt")

    # Test that input2 exists
    handler.assert_object_exists("input2.txt")

    # Test that input3 matches the expected content
    handler.assert_content_equals("input3.txt", "content")
```

Test conditional function invocations:

```py
# Test that a function completed
def test_run_on_true(tester: WorkflowTester):
    tester.wait_for("run_on_true")
    tester.assert_function_completed("run_on_true")


# Test that a function was not invoked
def test_dont_run_on_true(tester: WorkflowTester):
    tester.wait_for("dont_run_on_true")
    tester.assert_function_not_invoked("dont_run_on_true")
```

### Invoking Tests

Tests can either be invoked from the VS Code testing UI or from the command line:

```bash
pytest tests
```

**Helpful options:**

- **`-s`**: Capture output, including function logs.
- **`-v\-vv`**: Create verbose output for debugging complex assertions.
