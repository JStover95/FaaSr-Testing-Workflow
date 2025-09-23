FROM ghcr.io/faasr/github-actions-python:dev

# Copy the custom entrypoint and LocalScheduler
COPY local_docker/faasr_entry_local.py /action/faasr_entry_local.py
COPY scripts/local_scheduler.py /action/local_scheduler.py

# Install docker dependency for LocalScheduler
RUN pip install docker

# Override the base image's entrypoint to use our custom one
ENTRYPOINT ["python3", "faasr_entry_local.py"]
