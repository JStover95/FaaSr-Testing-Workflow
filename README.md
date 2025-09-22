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
   - Change the `Branch` of the `My_GitHub_Account` entry in `ComputeServers` to the branch you created in the previous step.

6. Commit and push your changes.

7. Register the workflow (via GitHub Actions or from the command line).

   ```bash
   ./register_workflow.sh --workflow-file <Your Workflow File>
   ```

8. Invoke the workflow with the workflow runner.

   ```bash
   python -m scripts.workflow_runner --workflow-file <Your Workflow File>
   ```

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
