# FaaSr Local Scheduler

The Local Scheduler allows you to run FaaSr workflows locally using Docker containers instead of GitHub Actions. This is useful for development, testing, and debugging workflows without requiring GitHub Actions API access.

## Features

- **Local Docker Execution**: Runs the same Docker containers locally that would run on GitHub Actions
- **Identical Environment**: Maintains the same environment variables and behavior as GitHub Actions
- **Real-time Logging**: Streams container logs in real-time for debugging
- **Automatic Cleanup**: Cleans up Docker containers after execution
- **Error Handling**: Robust error handling and logging

## Prerequisites

1. **Docker**: Docker must be installed and running on your system
2. **Python Dependencies**: Install the required Python packages:

   ```bash
   pip install docker
   ```

3. **Environment Variables**: Set up the required environment variables (see below)

## Installation

1. Ensure Docker is installed and running:

   ```bash
   docker --version
   docker ps
   ```

2. Install the Docker Python library:

   ```bash
   pip install docker
   ```

3. Set up your environment variables (see Environment Variables section)

## Environment Variables

The Local Scheduler requires the same environment variables that would be used in GitHub Actions:

```bash
# GitHub credentials
export GITHUB_TOKEN="your-github-personal-access-token"

# S3/MinIO credentials
export My_S3_Bucket_AccessKey="your-s3-access-key"
export My_S3_Bucket_SecretKey="your-s3-secret-key"

# Alternative MinIO credentials (if using MinIO)
export MINIO_ACCESS_KEY="your-minio-access-key"
export MINIO_SECRET_KEY="your-minio-secret-key"
```

## Usage

### Basic Usage

Run a workflow locally:

```bash
python scripts/local_scheduler.py --workflow-file main.json
```

### With Cleanup

Clean up Docker containers before exit:

```bash
python scripts/local_scheduler.py --workflow-file main.json --cleanup
```

### Programmatic Usage

```python
from local_scheduler import LocalWorkflowAdapter

# Create adapter
adapter = LocalWorkflowAdapter("main.json")

# Trigger workflow
adapter.trigger_workflow()
```

### Advanced Usage with Custom Scheduler

```python
from local_scheduler import LocalScheduler
from FaaSr_py.engine.faasr_payload import FaaSrPayload

# Create FaaSrPayload from workflow
faasr_payload = FaaSrPayload(workflow_data)

# Create local scheduler
scheduler = LocalScheduler(faasr_payload)

# Trigger specific function
scheduler.trigger_func("workflow_name", "function_name")
```

## How It Works

1. **Inheritance**: The `LocalScheduler` class inherits from the standard `Scheduler` class
2. **Override**: It overrides the `invoke_gh` method to replace GitHub Actions API calls with local Docker execution
3. **Environment**: It maintains the same environment variables and container configuration as GitHub Actions
4. **Execution**: It runs Docker containers locally with the same entry point (`faasr_entry.py`)

## Key Differences from GitHub Actions

| Aspect | GitHub Actions | Local Scheduler |
|--------|----------------|------------------|
| **Execution** | Remote GitHub servers | Local Docker containers |
| **API Calls** | GitHub API requests | Direct Docker execution |
| **Logs** | GitHub Actions logs | Real-time container logs |
| **Debugging** | Limited | Full local debugging |
| **Network** | GitHub's network | Your local network |
| **Cost** | GitHub Actions minutes | Local resources only |

## Container Management

The Local Scheduler automatically:

- Pulls required Docker images
- Runs containers with proper environment variables
- Streams logs in real-time
- Cleans up containers after execution
- Handles errors and timeouts

## Troubleshooting

### Docker Not Available

```plaintext
Error: Docker Python library not found
```

**Solution**: Install the Docker Python library:

```bash
pip install docker
```

### Container Execution Failed

```plaintext
Container execution failed with exit code 1
```

**Solution**: Check the container logs and ensure all environment variables are set correctly.

### Image Not Found

```plaintext
Container image not found: ghcr.io/faasr/github-actions-python:dev
```

**Solution**: Ensure you have access to the container registry and the image exists.

### Permission Denied

```plaintext
Permission denied while trying to connect to the Docker daemon
```

**Solution**: Ensure your user has permission to access Docker, or run with `sudo`.

## Example Workflow

Here's a complete example of running a workflow locally:

```python
#!/usr/bin/env python3
import os
from local_scheduler import LocalWorkflowAdapter

# Set environment variables
os.environ['GITHUB_TOKEN'] = 'your-token'
os.environ['My_S3_Bucket_AccessKey'] = 'your-access-key'
os.environ['My_S3_Bucket_SecretKey'] = 'your-secret-key'

# Create adapter and run workflow
adapter = LocalWorkflowAdapter("main.json")
adapter.trigger_workflow()
```

## Benefits

1. **Development**: Test workflows locally without GitHub Actions
2. **Debugging**: Full access to container logs and debugging
3. **Offline**: Run workflows without internet connectivity
4. **Cost**: No GitHub Actions minutes consumed
5. **Speed**: Faster iteration during development
6. **Control**: Full control over execution environment

## Limitations

1. **Network**: Local network instead of GitHub's network
2. **Secrets**: Must manage secrets locally instead of GitHub Secrets
3. **Scaling**: Limited by local machine resources
4. **Integration**: No integration with GitHub's workflow features

## Contributing

To extend the Local Scheduler:

1. Inherit from `LocalScheduler` class
2. Override specific methods as needed
3. Add custom Docker execution logic
4. Implement additional error handling

## Support

For issues and questions:

1. Check Docker is running: `docker ps`
2. Verify environment variables are set
3. Check container logs for errors
4. Ensure workflow JSON is valid
