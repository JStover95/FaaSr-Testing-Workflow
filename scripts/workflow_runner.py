import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
from enum import Enum

import boto3
from botocore.exceptions import ClientError
from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.invoke_workflow import WorkflowMigrationAdapter

LOGGER_NAME = "WorkflowRunner"
REQUIRED_ENV_VARS = [
    "MY_S3_BUCKET_ACCESSKEY",
    "MY_S3_BUCKET_SECRETKEY",
    "GITHUB_TOKEN",
    "GITHUB_REPOSITORY",
]


class InitializationError(Exception):
    """Exception raised for initialization errors"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"Error initializing workflow runner: {self.message}"


class FunctionStatus(Enum):
    """Test execution status"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class WorkflowRunner(WorkflowMigrationAdapter):
    failed_regex = re.compile(r"\[[\d\.]+?\] \[ERROR\]")

    def __init__(
        self,
        workflow_file_path: str,
        timeout: int,
        check_interval: int,
        stream_logs: bool = False,
    ):
        """
        Initialize the workflow runner.

        Args:
            workflow_file_path: Path to the FaaSr workflow JSON file
            timeout: Function timeout in seconds. If any function status does not change
                after this time, the workflow will timeout and exit.
            check_interval: Interval for checking the status of the workflow in seconds
            stream_logs: Whether to stream the logs of the workflow
        """
        super().__init__(workflow_file_path)

        self._validate_environment()

        # Setup logging
        self.timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H:%M:%S")
        self._setup_logging()

        self.function_statuses: dict[str, FunctionStatus] = {
            function_name: FunctionStatus.PENDING
            for function_name in self.workflow_data["ActionList"].keys()
        }
        self.function_logs: dict[str, list[str]] = {
            function_name: []
            for function_name in self.workflow_data["ActionList"].keys()
        }
        self.timeout = timeout
        self.seconds_since_last_status_change = 0
        self.check_interval = check_interval
        self.stream_logs = stream_logs

        # Thread management
        self._status_lock = threading.Lock()
        self._monitoring_thread = None
        self._monitoring_complete = False
        self._shutdown_requested = False
        self._cleanup_timeout = 30  # seconds to wait for graceful shutdown

        # Initialize S3 client for monitoring
        self.s3_client = None
        self.bucket_name = None
        self._init_s3_client()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    def _validate_environment(self):
        """Validate environment variables"""
        missing_env_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
        if missing_env_vars:
            raise InitializationError(
                f"Missing required environment variables: {', '.join(missing_env_vars)}"
            )

    def _setup_logging(self):
        """Setup logging configuration"""
        os.makedirs("logs", exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.logger = logging.getLogger(LOGGER_NAME)

    def _init_s3_client(self):
        """Initialize S3 client for monitoring"""
        self.logger.info("Initializing S3 client for monitoring")

        try:
            # Create a mock FaaSrPayload to get S3 credentials
            # In a real scenario, you'd get these from your workflow config
            default_datastore = self.workflow_data.get(
                "DefaultDataStore",
                "My_S3_Bucket",
            )
            datastore_config = self.workflow_data["DataStores"][default_datastore]

            if datastore_config.get("Endpoint"):
                self.s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=os.getenv("MY_S3_BUCKET_ACCESSKEY"),
                    aws_secret_access_key=os.getenv("MY_S3_BUCKET_SECRETKEY"),
                    region_name=datastore_config["Region"],
                    endpoint_url=datastore_config["Endpoint"],
                )
            else:
                self.s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=os.getenv("MY_S3_BUCKET_ACCESSKEY"),
                    aws_secret_access_key=os.getenv("MY_S3_BUCKET_SECRETKEY"),
                    region_name=datastore_config["Region"],
                )

            self.bucket_name = datastore_config["Bucket"]
            self.logger.info(f"Initialized S3 client for bucket: {self.bucket_name}")

        except Exception as e:
            self.logger.error(f"Failed to initialize S3 client: {e}")
            raise InitializationError(f"Failed to initialize S3 client: {e}")

    def _monitor_workflow_execution(self):
        """Monitor the workflow execution.
        
        This method will monitor the workflow execution and update the function statuses.
        It will also stream the logs of the functions if the stream_logs flag is set.
        """
        self.logger.info(
            f"Monitoring workflow execution for functions: {', '.join(self.function_statuses.keys())}"
        )

        last_status_change_time = time.time()

        while self.seconds_since_last_status_change < self.timeout and not self._shutdown_requested:
            all_completed = True
            failed = False

            # Get a snapshot of current statuses
            with self._status_lock:
                functions_to_check = [
                    (name, status)
                    for name, status in self.function_statuses.items()
                    if status == FunctionStatus.PENDING
                    or status == FunctionStatus.RUNNING
                ]

            # Check completion status for each function
            for function_name, status in functions_to_check:
                if self.stream_logs and status == FunctionStatus.RUNNING:
                    self._update_function_logs(function_name)

                all_completed = False
                if status == FunctionStatus.PENDING and self._check_function_running(
                    function_name
                ):
                    self.seconds_since_last_status_change = 0
                    last_status_change_time = time.time()
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.RUNNING
                    self.logger.info(f"Function {function_name} running")
                elif status == FunctionStatus.RUNNING and self._check_function_failed(
                    function_name
                ):
                    self.seconds_since_last_status_change = 0
                    last_status_change_time = time.time()
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.FAILED
                    self.logger.info(f"Function {function_name} failed")
                    failed = True
                elif (
                    status == FunctionStatus.RUNNING
                    and self._check_function_completion(function_name)
                ):
                    self.seconds_since_last_status_change = 0
                    last_status_change_time = time.time()
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.COMPLETED
                    self.logger.info(f"Function {function_name} completed")
                else:
                    self.seconds_since_last_status_change = time.time() - last_status_change_time

            if all_completed:
                self.logger.info("All functions completed")
                break

            # Cascade failed status to all pending functions
            if failed:
                with self._status_lock:
                    for function_name, status in self.function_statuses.items():
                        if status == FunctionStatus.PENDING:
                            self.logger.info(
                                f"Skipping function {function_name} on failure"
                            )
                            self.function_statuses[function_name] = (
                                FunctionStatus.SKIPPED
                            )
                break

            time.sleep(self.check_interval)

        # Check for timeouts or shutdown
        with self._status_lock:
            for function_name, status in self.function_statuses.items():
                if status == FunctionStatus.PENDING or status == FunctionStatus.RUNNING:
                    if self._shutdown_requested:
                        self.function_statuses[function_name] = FunctionStatus.SKIPPED
                        self.logger.info(
                            f"Function {function_name} skipped due to shutdown"
                        )
                    else:
                        self.function_statuses[function_name] = FunctionStatus.TIMEOUT
                        self.logger.warning(f"Function {function_name} timed out")

        # Mark monitoring as complete
        with self._status_lock:
            self._monitoring_complete = True

    def _update_function_logs(self, function_name: str) -> list[str]:
        """Update the logs for a function"""
        invocation_folder = get_invocation_folder(self.faasr_payload)
        key = f"{invocation_folder}/{function_name}.txt"
        log_content = self.s3_client.get_object(
            Bucket=self.bucket_name,
            Key=str(key),
        )
        new_logs = log_content["Body"].read().decode("utf-8").strip().split("\n")
        num_existing_logs = len(self.function_logs[function_name])
        for i in range(num_existing_logs, len(new_logs)):
            print(new_logs[i])
        self.function_logs[function_name] = new_logs

    def _check_function_running(self, function_name: str) -> bool:
        """Check if a function is running by looking for log files in S3"""
        try:
            # Get the invocation folder path
            invocation_folder = get_invocation_folder(self.faasr_payload)

            # Check for log files
            key = f"{invocation_folder}/{function_name}.txt"

            try:
                self.s3_client.head_object(
                    Bucket=self.bucket_name,
                    Key=str(key),
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    return False
                else:
                    raise e

            return True

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking running status for {function_name}: {e}")
            return False

    def _check_function_failed(self, function_name: str) -> bool:
        try:
            logs = "\n".join(self.function_logs[function_name])
            return self.failed_regex.search(logs) is not None

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking failed status for {function_name}: {e}")
            return False

    def _check_function_completion(self, function_name: str) -> bool:
        """Check if a function has completed by looking for .done file in S3"""
        try:
            # Get the invocation folder path
            invocation_folder = get_invocation_folder(self.faasr_payload)

            # Check for .done file
            key = f"{invocation_folder}/function_completions/{function_name}.done"

            # List objects with this prefix
            try:
                self.s3_client.head_object(
                    Bucket=self.bucket_name,
                    Key=str(key),
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    return False
                else:
                    raise e

            return True

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking completion for {function_name}: {e}")
            return False

    def _set_function_status(self, function_name: str, status: FunctionStatus):
        """Set the status of a function (thread-safe)"""
        with self._status_lock:
            self.function_statuses[function_name] = status

    def get_function_statuses(self) -> dict[str, FunctionStatus]:
        """Get a thread-safe copy of function statuses"""
        with self._status_lock:
            return self.function_statuses.copy()

    def is_monitoring_complete(self) -> bool:
        """Check if monitoring is complete (thread-safe)"""
        with self._status_lock:
            return self._monitoring_complete

    def wait_for_completion(self, timeout: float = None) -> bool:
        """Wait for monitoring to complete (thread-safe)"""
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=timeout)
            return not self._monitoring_thread.is_alive()
        return True

    def shutdown(self, timeout: float = None) -> bool:
        """
        Gracefully shutdown the monitoring thread.

        Args:
            timeout: Maximum time to wait for graceful shutdown (default: self._cleanup_timeout)

        Returns:
            bool: True if shutdown was successful, False if timeout occurred
        """
        if not self._monitoring_thread or not self._monitoring_thread.is_alive():
            return True

        self.logger.info("Requesting graceful shutdown of monitoring thread...")

        # Signal shutdown request
        with self._status_lock:
            self._shutdown_requested = True

        # Wait for thread to finish gracefully
        wait_timeout = timeout if timeout is not None else self._cleanup_timeout
        self._monitoring_thread.join(timeout=wait_timeout)

        if self._monitoring_thread.is_alive():
            self.logger.warning(
                f"Monitoring thread did not shutdown within {wait_timeout}s"
            )
            return False
        else:
            self.logger.info("Monitoring thread shutdown successfully")
            return True

    def force_shutdown(self):
        """
        Force shutdown of the monitoring thread.
        This should only be used as a last resort.
        """
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            self.logger.warning("Force shutting down monitoring thread...")
            # Note: Python threads cannot be forcefully killed, but we can mark as shutdown
            with self._status_lock:
                self._shutdown_requested = True
                self._monitoring_complete = True

    def cleanup(self):
        """
        Comprehensive cleanup of all resources.
        This method should be called when the runner is no longer needed.
        """
        self.logger.info("Starting cleanup process...")

        # Try graceful shutdown first
        if not self.shutdown():
            self.logger.warning("Graceful shutdown failed, forcing shutdown...")
            self.force_shutdown()

        # Close S3 client if it exists
        if self.s3_client:
            try:
                # boto3 clients don't need explicit closing, but we can clear the reference
                self.s3_client = None
                self.logger.info("S3 client cleaned up")
            except Exception as e:
                self.logger.error(f"Error cleaning up S3 client: {e}")

        self.logger.info("Cleanup completed")

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown on interruption"""

        def signal_handler(signum, frame):
            self.logger.info(
                f"Received signal {signum}, initiating graceful shutdown..."
            )
            self.shutdown()
            sys.exit(0)

        # Register signal handlers for common interruption signals
        signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # Termination request

    def _create_faasr_payload_from_local_file(self):
        workflow = super()._create_faasr_payload_from_local_file()

        # Set the InvocationID and InvocationTimestamp for monitoring
        workflow["InvocationID"] = str(uuid.uuid4())
        workflow["InvocationTimestamp"] = self.timestamp

        return workflow

    def trigger_workflow(self):
        super().trigger_workflow()

        # Start monitoring in background thread
        self._monitoring_thread = threading.Thread(
            target=self._monitor_workflow_execution, daemon=True
        )
        self._monitoring_thread.start()

        return True


def main():
    """Example of using the thread-safe WorkflowRunner"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-file", type=str, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--check-interval", type=int, default=1)
    parser.add_argument("--stream-logs", type=bool, default=True)
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    # Initialize the workflow runner
    runner = WorkflowRunner(
        workflow_file_path=args.workflow_file,
        timeout=args.timeout,
        check_interval=args.check_interval,
        stream_logs=args.stream_logs,
    )

    # Start the workflow (returns immediately)
    print("🚀 Starting workflow...")
    runner.trigger_workflow()
    print("✅ Workflow started, monitoring in background")

    # Monitor status changes
    print("\n📊 Monitoring function statuses...")
    previous_statuses = {}

    while not runner.is_monitoring_complete():
        # Get current statuses (thread-safe)
        current_statuses = runner.get_function_statuses()

        # Check for changes
        for function_name, status in current_statuses.items():
            if (
                function_name not in previous_statuses
                or previous_statuses[function_name] != status
            ):
                match status:
                    case FunctionStatus.PENDING:
                        print(f"  {function_name}: {status.value}")
                    case FunctionStatus.RUNNING:
                        print(f"🔄 {function_name}: {status.value}")
                    case FunctionStatus.COMPLETED:
                        print(f"✅ {function_name}: {status.value}")
                    case FunctionStatus.FAILED:
                        print(f"‼️ {function_name}: {status.value}")
                    case FunctionStatus.SKIPPED:
                        print(f"  {function_name}: {status.value}")
                    case FunctionStatus.TIMEOUT:
                        print(f"  {function_name}: {status.value}")

        previous_statuses = current_statuses.copy()

        # Wait before next check
        time.sleep(1)

    # Get final results
    final_statuses = runner.get_function_statuses()
    print("\n🏁 Final Results:")
    for function_name, status in final_statuses.items():
        print(f"  {function_name}: {status.value}")

    # Check overall success
    all_completed = all(
        status == FunctionStatus.COMPLETED for status in final_statuses.values()
    )

    if all_completed:
        print("\n✅ All functions completed successfully!")
    else:
        print("\n❌ Some functions failed or timed out")

    return 0 if all_completed else 1


if __name__ == "__main__":
    exit(main())
