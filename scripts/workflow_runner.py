from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


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
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from enum import Enum

import boto3
from botocore.exceptions import ClientError
from FaaSr_py.helpers.graph_functions import build_adjacency_graph
from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.function_logger import FunctionLogger, InvocationStatus
from scripts.invoke_workflow import WorkflowMigrationAdapter
from scripts.utils import extract_function_name, get_s3_path

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
    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


def pending(status: FunctionStatus) -> bool:
    return status == FunctionStatus.PENDING


def invoked(status: FunctionStatus) -> bool:
    return status == FunctionStatus.INVOKED


def not_invoked(status: FunctionStatus) -> bool:
    return status == FunctionStatus.NOT_INVOKED


def running(status: FunctionStatus) -> bool:
    return status == FunctionStatus.RUNNING


def completed(status: FunctionStatus) -> bool:
    return status == FunctionStatus.COMPLETED


def failed(status: FunctionStatus) -> bool:
    return status == FunctionStatus.FAILED


def skipped(status: FunctionStatus) -> bool:
    return status == FunctionStatus.SKIPPED


def timed_out(status: FunctionStatus) -> bool:
    return status == FunctionStatus.TIMEOUT


def has_run(status: FunctionStatus) -> bool:
    return not (pending(status) or invoked(status) or not_invoked(status))


def has_completed(status: FunctionStatus) -> bool:
    return completed(status) or not_invoked(status)


class StopMonitoring(Exception):
    """Exception raised to stop monitoring"""


class WorkflowRunner(WorkflowMigrationAdapter):
    failed_regex = re.compile(r"\[[\d\.]+?\] \[ERROR\]")
    invoked_regex = re.compile(r"(?<=Successfully invoked: )[a-zA-Z\-_]+")

    def __init__(
        self,
        *,
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
                after this time, the workflow will timeout and exit
            check_interval: Interval for checking the status of the workflow in seconds
            stream_logs: Whether to stream the logs of the workflow
        """
        super().__init__(workflow_file_path)

        self._validate_environment()

        # Setup logging
        self.timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
        self.logger = self._setup_logger()

        # Initialize S3 client for monitoring
        self.s3_client, self.bucket_name = self._init_s3_client()

        # Initialize function loggers
        self.function_logs: dict[str, FunctionLogger] = {}
        self.stream_logs = stream_logs

        # Monitoring parameters
        self.timeout = timeout
        self.check_interval = check_interval
        self.last_status_change_time: float = time.time()
        self.seconds_since_last_status_change: float = 0.0

        # Thread management
        self._status_lock = threading.Lock()
        self._monitoring_thread = None
        self._monitoring_complete = False
        self._shutdown_requested = False
        self._cleanup_timeout = 30  # seconds to wait for graceful shutdown

        # Build adjacency graph for monitoring
        self.adj_graph, self.ranks = build_adjacency_graph(self.workflow_data)
        self.reverse_adj_graph = self._build_reverse_adjacency_graph()

        # Initialize function statuses
        self.workflow_name = self.workflow_data.get("WorkflowName")
        self.workflow_invoke = self.workflow_data.get("FunctionInvoke")
        self.function_names = self.workflow_data["ActionList"].keys()
        self.function_statuses = self._build_function_statuses()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    ##########################
    # Initialization helpers #
    ##########################
    def _validate_environment(self):
        """Validate environment variables"""
        missing_env_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
        if missing_env_vars:
            raise InitializationError(
                f"Missing required environment variables: {', '.join(missing_env_vars)}"
            )

    def _setup_logger(self):
        """Setup logging configuration"""
        # Use the existing logger configuration
        logger = logging.getLogger("WorkflowRunner")
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(levelname)s] [%(filename)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def _init_s3_client(self) -> tuple[S3Client, str]:
        """Initialize S3 client for monitoring"""
        self.logger.info("Initializing S3 client for monitoring")

        try:
            default_datastore = self.workflow_data.get(
                "DefaultDataStore",
                "My_S3_Bucket",
            )
            datastore_config = self.workflow_data["DataStores"][default_datastore]

            if datastore_config.get("Endpoint"):
                s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=os.getenv("MY_S3_BUCKET_ACCESSKEY"),
                    aws_secret_access_key=os.getenv("MY_S3_BUCKET_SECRETKEY"),
                    region_name=datastore_config["Region"],
                    endpoint_url=datastore_config["Endpoint"],
                )
            else:
                s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=os.getenv("MY_S3_BUCKET_ACCESSKEY"),
                    aws_secret_access_key=os.getenv("MY_S3_BUCKET_SECRETKEY"),
                    region_name=datastore_config["Region"],
                )

            bucket_name = datastore_config["Bucket"]
            self.logger.info(f"Initialized S3 client for bucket: {bucket_name}")

            return s3_client, bucket_name

        except Exception as e:
            self.logger.error(f"Failed to initialize S3 client: {e}")
            raise InitializationError(f"Failed to initialize S3 client: {e}")

    def _build_reverse_adjacency_graph(self):
        """Build the reverse adjacency graph"""
        reverse_adj_graph = defaultdict(set)
        for invoker, invoked_functions in self.adj_graph.items():
            for invoked in invoked_functions:
                reverse_adj_graph[invoked].add(invoker)
        return reverse_adj_graph

    def _build_function_statuses(self):
        """Build the function names"""
        statuses = {}
        for function_name in self.function_names:
            if function_name == self.workflow_invoke and self.ranks[function_name] <= 1:
                statuses[function_name] = FunctionStatus.INVOKED
            elif function_name == self.workflow_invoke:
                for rank in self._iter_ranks(function_name):
                    statuses[rank] = FunctionStatus.INVOKED
            elif self.ranks[function_name] <= 1:
                statuses[function_name] = FunctionStatus.PENDING
            else:
                for rank in self._iter_ranks(function_name):
                    statuses[rank] = FunctionStatus.PENDING
        return statuses

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown on interruption"""

        def signal_handler(signum, frame):
            signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(signum))
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(signum))
            self.logger.info(
                f"Received signal {signum}, initiating graceful shutdown..."
            )
            self.shutdown()
            sys.exit(0)

        # Register signal handlers for common interruption signals
        signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # Termination request

    #########################
    # Thread-safe interface #
    #########################
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

    #######################
    # Workflow monitoring #
    #######################
    def _start_monitoring(self):
        self.logger.info(
            f"Monitoring workflow execution for functions: {', '.join(self.function_statuses.keys())}"
        )

        self._reset_timer()

        while not self._did_timeout() and not self._shutdown_requested:
            try:
                self._monitor_workflow_execution()
            except StopMonitoring:
                break

        self._finish_monitoring()

    def _monitor_workflow_execution(self):
        failed = False

        # Check completion status for each function
        for function_name, status in self.get_function_statuses().items():
            if has_run(status) and function_name not in self.function_logs:
                self._start_function_logger(function_name)

            if pending(status):
                self._handle_pending(function_name)
            elif invoked(status) and self._check_function_running(function_name):
                self._handle_invoked(function_name)
            elif running(status) and self._check_function_failed(function_name):
                self._handle_failed(function_name)
                failed = True
            elif running(status) and self._check_function_completed(function_name):
                self._handle_completed(function_name)

        if all(has_completed(status) for status in self.function_statuses.values()):
            self.logger.info("All functions completed")
            raise StopMonitoring("All functions completed")

        # Cascade failed status to all pending functions
        if failed:
            self._cascade_failure()
            raise StopMonitoring("Failure detected")

        self._increment_timer()
        time.sleep(self.check_interval)

    def _finish_monitoring(self) -> None:
        # Check for timeouts or shutdown
        with self._status_lock:
            for function_name, status in self.function_statuses.items():
                if not has_completed(status) and self._shutdown_requested:
                    self.function_statuses[function_name] = FunctionStatus.SKIPPED
                    self.logger.info(
                        f"Function {function_name} skipped due to shutdown"
                    )
                elif not has_completed(status):
                    self.function_statuses[function_name] = FunctionStatus.TIMEOUT
                    self.logger.warning(f"Function {function_name} timed out")

        # Mark monitoring as complete
        with self._status_lock:
            self._monitoring_complete = True

        self.logger.info("Monitoring complete")

    ######################
    # Monitoring helpers #
    ######################
    def _reset_timer(self) -> None:
        self.last_status_change_time = time.time()
        self.seconds_since_last_status_change = 0.0

    def _increment_timer(self) -> None:
        self.seconds_since_last_status_change = (
            time.time() - self.last_status_change_time
        )

    def _did_timeout(self) -> bool:
        return self.seconds_since_last_status_change >= self.timeout

    def _start_function_logger(self, function_name: str) -> None:
        function_logger = FunctionLogger(
            function_name=function_name,
            workflow_name=self.workflow_name,
            invocation_folder=get_invocation_folder(self.faasr_payload),
            bucket_name=self.bucket_name,
            s3_client=self.s3_client,
            stream_logs=self.stream_logs,
        )
        function_logger.start()
        self.function_logs[function_name] = function_logger
        self.logger.info(f"Started function logger for {function_name}")

    def _handle_pending(self, function_name: str) -> None:
        invocation_status = self._get_invocation_status(function_name)
        if invocation_status == InvocationStatus.INVOKED:
            self._reset_timer()
            with self._status_lock:
                self.function_statuses[function_name] = FunctionStatus.INVOKED
            self.logger.info(f"Function {function_name} invoked")
        elif invocation_status == InvocationStatus.NOT_INVOKED:
            self._reset_timer()
            with self._status_lock:
                self.function_statuses[function_name] = FunctionStatus.NOT_INVOKED
            self.logger.info(f"Function {function_name} not invoked")

    def _handle_invoked(self, function_name: str) -> None:
        self._reset_timer()
        with self._status_lock:
            self.function_statuses[function_name] = FunctionStatus.RUNNING
        self.logger.info(f"Function {function_name} running")

    def _handle_completed(self, function_name: str) -> None:
        self._reset_timer()
        with self._status_lock:
            self.function_statuses[function_name] = FunctionStatus.COMPLETED
        self.function_logs[function_name].set_function_complete()
        self.logger.info(f"Function {function_name} completed")

    def _handle_failed(self, function_name: str) -> None:
        self._reset_timer()
        with self._status_lock:
            self.function_statuses[function_name] = FunctionStatus.FAILED
        self.logger.info(f"Function {function_name} failed")

    def _cascade_failure(self) -> None:
        with self._status_lock:
            for function_name, status in self.function_statuses.items():
                if not has_completed(status):
                    self.function_statuses[function_name] = FunctionStatus.SKIPPED
                    self.logger.info(f"Skipping function {function_name} on failure")

    def _check_function_running(self, function_name: str) -> bool:
        """Check if a function is running by looking for log files in S3"""
        try:
            invocation_folder = get_invocation_folder(self.faasr_payload)
            key = get_s3_path(f"{invocation_folder}/{function_name}.txt")
            return self._check_object_exists(key)

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking running status for {function_name}: {e}")
            return False

    def _check_function_failed(self, function_name: str) -> bool:
        with suppress(KeyError):
            return self.function_logs[function_name].function_failed
        return False

    def _check_function_completed(self, function_name: str) -> bool:
        """Check if a function has completed by looking for .done file in S3"""
        try:
            invocation_folder = get_invocation_folder(self.faasr_payload)
            s3_function_name = function_name.replace("(", ".").replace(")", "")
            key = get_s3_path(
                f"{invocation_folder}/function_completions/{s3_function_name}.done"
            )
            return self._check_object_exists(key)

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking completion for {function_name}: {e}")
            return False

    def _set_function_status(self, function_name: str, status: FunctionStatus):
        """Set the status of a function (thread-safe)"""
        with self._status_lock:
            self.function_statuses[function_name] = status

    #####################
    # Thread management #
    #####################
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

        if self._monitoring_thread and self._monitoring_thread.is_alive():
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
            self.logger.warning("Force shutting down monitoring and logs threads...")
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

    #############
    # Overrides #
    #############
    def _create_faasr_payload_from_local_file(self):
        workflow = super()._create_faasr_payload_from_local_file()

        # Set the InvocationID and InvocationTimestamp for monitoring
        workflow["InvocationID"] = str(uuid.uuid4())
        workflow["InvocationTimestamp"] = self.timestamp

        return workflow

    def trigger_workflow(self):
        super().trigger_workflow()

        self.logger.info(
            f"Workflow {self.workflow_name} triggered with InvocationID: {self.faasr_payload['InvocationID']}"
        )

        # Start monitoring in background thread
        self._monitoring_thread = threading.Thread(
            target=self._start_monitoring,
            daemon=True,
        )
        self._monitoring_thread.start()

        return True

    ###################
    # Private helpers #
    ###################
    def _iter_ranks(self, function_name: str) -> Generator[str]:
        """Iterate over the ranks of a function"""
        for rank in range(1, self.ranks[function_name] + 1):
            yield f"{function_name}({rank})"

    def _check_object_exists(self, key: str) -> bool:
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=str(key))
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise e
        return True

    def _get_invocation_status(self, function_name: str) -> InvocationStatus:
        """Check if a function was invoked by looking for log files in S3"""
        for invoker in self.reverse_adj_graph[extract_function_name(function_name)]:
            with suppress(KeyError):
                status = self.function_logs[invoker].get_invocation_status(
                    function_name
                )
                if status == InvocationStatus.INVOKED:
                    return InvocationStatus.INVOKED
                elif status == InvocationStatus.NOT_INVOKED:
                    return InvocationStatus.NOT_INVOKED
        return InvocationStatus.PENDING


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
                        print(f"⏳ {function_name}: {status.value}")
                    case FunctionStatus.INVOKED:
                        print(f"🚀 {function_name}: {status.value}")
                    case FunctionStatus.NOT_INVOKED:
                        print(f"ℹ️ {function_name}: {status.value}")
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
        status in {FunctionStatus.COMPLETED, FunctionStatus.NOT_INVOKED}
        for status in final_statuses.values()
    )

    if all_completed:
        print("\n✅ All functions completed successfully!")
    else:
        print("\n❌ Some functions failed or timed out")

    return 0 if all_completed else 1


if __name__ == "__main__":
    exit(main())
