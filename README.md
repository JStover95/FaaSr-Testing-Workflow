# FaaSr-Testing-Workflow

This repo includes workflows for FaaSr integration testing:

**Workflows:**

- **`main.json`**: The complete integration testing workflow.

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

5. Make a copy of `main.json` and give it a recognizable name
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
- **Function Status Tracking**: Tracks individual function states (pending, running, completed, failed, skipped, timeout)
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

- **`PENDING`**: The function is waiting to start
- **`RUNNING`**: The function is currently executing
- **`COMPLETED`**: The function finished successfully
- **`FAILED`**: The function encountered an error
- **`SKIPPED`**: The function was skipped due to upstream failure
- **`TIMEOUT`**: The function was pending or running when the workflow timed out.

### Environment Variables

The following environment variables are required:

- `MY_S3_BUCKET_ACCESSKEY`: S3 access key for monitoring
- `MY_S3_BUCKET_SECRETKEY`: S3 secret key for monitoring
- `GITHUB_TOKEN`: GitHub personal access token
- `GITHUB_REPOSITORY`: GitHub repository name

### Status Monitoring

The Workflow Runner continuously checks function execution status.

- All functions are labeled as `PENDING` upon invocation.
- A function is labeled as `RUNNING` when a log file is created.
- A function is labeled as `COMPLETE` when a `.done` file is created.
- A function is labeled as `FAILED` when an `ERROR` log output is generated.
- A function is labeled as `SKIPPED` when an upstream failure occurs.
- A function is labeled as `TIMEOUT` when the workflow times out before the function completes or fails.

### Thread Safety

The Workflow Runner is designed to be thread-safe:

- All status updates are protected by locks
- Safe for concurrent access from multiple threads
- Graceful shutdown handling prevents race conditions
- Clean resource management and cleanup

## Writing Tests

A `handler` fixture can be used that automatically invokes the workflow when running tests.

- **`handler.wait_for`**: Wait for the function to complete. An exception is thrown when a function fails, causing the test to fail automatically.
- **`handler.get_s3_key`**: Get full S3 key for a given file.

Use the `s3_client` fixture to run tests against function outputs on S3.

- **`s3_client.head_object`**: Test whether an object exists.
- **`s3_client.get_object`**: Test against the contents of an object.

For example:

```py
def test_py_api(handler: WorkflowHandler, s3_client: S3Client):
    handler.wait_for("test_py_api")

    input1 = handler.get_s3_key("input1.txt")
    input2 = handler.get_s3_key("input2.txt")
    input3 = handler.get_s3_key("input3.txt")
    
    # Test that input1 does not exist
    with pytest.raises(Exception):
        s3_client.head_object(Bucket=handler.bucket_name, Key=input1)

    # Test that input2 exists
    assert s3_client.head_object(Bucket=handler.bucket_name, Key=input2) is not None

    # Test that input3 matches the expected content
    assert s3_client.get_object(Bucket=handler.bucket_name, Key=input3)["Body"].read() == b"input3"
```

## Invoking Tests

Tests can either be invoked from the VS Code testing UI or from the command line:

```bash
pytest tests
```

**Helpful options:**

- **`-s`**: Capture output, including function logs.
- **`-v\-vv`**: Create verbose output for debugging complex assertions.
