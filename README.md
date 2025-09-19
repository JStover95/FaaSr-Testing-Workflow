# FaaSr-Testing-Workflow

This repo includes workflows for FaaSr integration testing:

**Workflows:**

- **`main.json`**: The complete integration testing workflow.

## Getting Started

1. Set up the Python virtual environment. Python 3.13 is recommended.

   ```bash
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Make a copy of `.env.template` and save it as `.env`. Save your GitHub PAT as `GITHUB_TOKEN`.

3. Make a copy of `main.json` and give it a recognizable name and change the `WorkflowName` attribute to a unique name.

4. Register the workflow (via GitHub Actions or from the command line).

   ```bash
   ./register_workflow.sh --workflow-file <Your Workflow File>
   ```

5. Invoke the workflow with the integration test helper.

   ```bash
   python -m scripts.invoke_integration_tests --workflow-file <Your Workflow File>
   ```
