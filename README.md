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

2. Register a workflow (via GitHub Actions or from the command line).

   ```bash
   python scripts/register_workflow.py --workflow-file <Workflow JSON File>
   ```

3. Invoke the workflow (via GitHub Actions or from the command line).

   ```bash
   python scripts/invoke_workflow.py --workflow-file <Workflow JSON File>
   ```
