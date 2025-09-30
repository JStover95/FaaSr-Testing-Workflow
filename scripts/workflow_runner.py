from typing import TYPE_CHECKING, Any, Generator, Literal

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


import logging
import os
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
    """Exception raised for WorkflowRunner initialization errors"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"Error initializing workflow runner: {self.message}"


class StopMonitoring(Exception):
    """Exception raised to stop WorkflowRunner monitoring"""


class FunctionStatus(Enum):
    """Function execution status"""

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


def has_final_state(status: FunctionStatus) -> bool:
    return (
        completed(status)
        or not_invoked(status)
        or failed(status)
        or timed_out(status)
        or skipped(status)
    )


class WorkflowRunner(WorkflowMigrationAdapter):
    logger_name = "WorkflowRunner"

    def __init__(
        self,
        *,
        workflow_file_path: str,
        timeout: int,
        check_interval: int,
        stream_logs: bool = False,
    ):
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
        self.last_change_time: float = time.time()
        self.seconds_since_last_change: float = 0.0

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
        self._function_statuses = self._build_function_statuses()

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    ##########################
    # Initialization helpers #
    ##########################
    def _validate_environment(self) -> None:
        """
        Validate required environment variables.

        Raises:
            InitializationError: If any required environment variables are missing.
        """
        missing_env_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
        if missing_env_vars:
            raise InitializationError(
                f"Missing required environment variables: {', '.join(missing_env_vars)}"
            )

    def _setup_logger(self) -> logging.Logger:
        """
        Initialize the WorkflowRunner logger.

        Returns:
            logging.Logger: The logger for the WorkflowRunner.
        """
        logger = logging.getLogger(self.logger_name)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(levelname)s] [%(filename)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def _init_s3_client(self) -> tuple[S3Client, str]:
        """
        Initialize an S3 client using the default data store configuration.

        Returns:
            tuple[S3Client, str]: The S3 client and bucket name.

        Raises:
            InitializationError: If the S3 client initialization fails.
        """
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

    def _build_reverse_adjacency_graph(self) -> dict[str, set[str]]:
        """
        Initialize the reverse adjacency graph:

        ```py
        {
            "invoker": [
                "invoked_function",
                "invoked_function",
                ...
            ]
        }
        ```

        Returns:
            dict[str, set[str]]: The reverse adjacency graph.
        """
        reverse_adj_graph = defaultdict(set)
        for invoker, invoked_functions in self.adj_graph.items():
            for invoked in invoked_functions:
                reverse_adj_graph[invoked].add(invoker)
        return reverse_adj_graph

    def _build_function_statuses(self) -> dict[str, FunctionStatus]:
        """
        Initialize the function statuses:

        ```py
        {
            "function_name": "status",
            "ranked_function(1)": "status",
            "ranked_function(2)": "status",
            ...
        }
        ```

        - The `WorkflowInvoke` function is initially set to `INVOKED`.
        - All other functions are initially set to `PENDING`.
        - Ranked functions include the rank in the function name.

        Returns:
            dict[str, FunctionStatus]: The function statuses.
        """
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

    def _setup_signal_handlers(self) -> None:
        """
        Setup signal handlers for graceful shutdown on interruption.

        - Control-C and SIGTERM will initiate a graceful shutdown.
        - A second signal will force shutdown.
        """

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
        """
        Get a copy of function statuses (thread-safe).

        Returns:
            dict[str, FunctionStatus]: A copy of the function statuses.
        """
        with self._status_lock:
            return self._function_statuses.copy()

    @property
    def monitoring_complete(self) -> bool:
        """
        Check if monitoring is complete (thread-safe).

        Returns:
            bool: True if monitoring is complete, False otherwise.
        """
        with self._status_lock:
            return self._monitoring_complete

    @property
    def shutdown_requested(self) -> bool:
        """
        Check if a shutdown request has been made (thread-safe).

        Returns:
            bool: True if a shutdown request has been made, False otherwise.
        """
        with self._status_lock:
            return self._shutdown_requested

    def _set_monitoring_complete(self) -> None:
        """Set the monitoring complete status to True (thread-safe)."""
        with self._status_lock:
            self._monitoring_complete = True

    def _set_shutdown_requested(self) -> None:
        """Set the shutdown requested status to True (thread-safe)."""
        with self._status_lock:
            self._shutdown_requested = True

    def _set_status(self, function_name: str, status: FunctionStatus) -> None:
        """
        Set the status of a function (thread-safe).

        Args:
            function_name: The name of the function to set the status of.
            status: The status to set the function to.
        """
        with self._status_lock:
            self._function_statuses[function_name] = status

    #######################
    # Workflow monitoring #
    #######################
    def _start_monitoring(self) -> None:
        """
        Start workflow monitoring. This:

        - Resets the monitoring timer.
        - Calls `_monitor_workflow_execution` in a loop until:
            - The monitoring timer times out.
            - A shutdown request is set.
            - `StopMonitoring` is raised.
        - Calls `_finish_monitoring` when the monitoring is complete.
        """
        self.logger.info(
            f"Monitoring workflow execution for functions: {', '.join(self.get_function_statuses().keys())}"
        )

        self._reset_timer()

        while not self._did_timeout() and not self.shutdown_requested:
            try:
                self._monitor_workflow_execution()
            except StopMonitoring:
                break

            self._increment_timer()
            time.sleep(self.check_interval)

        self._finish_monitoring()

    def _monitor_workflow_execution(self):
        """
        Monitor the workflow execution. This:

        - Checks the completion status for each function.
        - Starts a function logger if the function has run and no logger exists.
        - Handles changes in each function status.
        - Cascades a failure to all pending functions when any function fails.

        Raises:
            StopMonitoring: If all functions have successfully completed or a failure has been detected.
        """
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

        if all(
            has_completed(status) for status in self.get_function_statuses().values()
        ):
            self.logger.info("All functions completed")
            raise StopMonitoring("All functions completed")

        # Cascade failed status to all pending functions
        if failed:
            self._cascade_failure()
            raise StopMonitoring("Failure detected")

    def _finish_monitoring(self) -> None:
        """
        Finish monitoring. This:

        - Checks for any functions that have not completed and:
           - Sets the function status to `SKIPPED` if a shutdown was requested.
           - Otherwise, sets the function status to `TIMEOUT`.
        - Marks the monitoring as complete.
        """
        # Check for timeouts or shutdown
        for function_name, status in self.get_function_statuses().items():
            if not has_final_state(status) and self.shutdown_requested:
                self._set_status(function_name, FunctionStatus.SKIPPED)
                self.logger.info(f"Function {function_name} skipped due to shutdown")
            elif not has_final_state(status):
                self._set_status(function_name, FunctionStatus.TIMEOUT)
                self.logger.warning(f"Function {function_name} timed out")

        # Mark monitoring as complete
        self._set_monitoring_complete()

        self.logger.info("Monitoring complete")

    ######################
    # Monitoring helpers #
    ######################
    def _reset_timer(self) -> None:
        """Reset the monitoring timer."""
        self.last_change_time = time.time()
        self.seconds_since_last_change = 0.0

    def _increment_timer(self) -> None:
        """Increment the monitoring timer."""
        self.seconds_since_last_change = time.time() - self.last_change_time

    def _did_timeout(self) -> bool:
        """
        Check if the monitoring timer has timed out.

        Returns:
            bool: True if the monitoring timer has timed out, False otherwise.
        """
        return self.seconds_since_last_change >= self.timeout

    def _start_function_logger(self, function_name: str) -> None:
        """
        Start a function's logger and register it with the WorkflowRunner.

        Args:
            function_name: The name of the function to start the logger for.
        """
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
        """
        Handle a pending function.

        This sets the function status to `INVOKED` if the function was invoked,
        and `NOT_INVOKED` if the function was not invoked.

        Args:
            function_name: The name of the function to handle.
        """
        invocation_status = self._check_invocation_status(function_name)
        if invocation_status == InvocationStatus.INVOKED:
            self._reset_timer()
            self._set_status(function_name, FunctionStatus.INVOKED)
            self.logger.info(f"Function {function_name} invoked")
        elif invocation_status == InvocationStatus.NOT_INVOKED:
            self._reset_timer()
            self._set_status(function_name, FunctionStatus.NOT_INVOKED)
            self.logger.info(f"Function {function_name} not invoked")

    def _handle_invoked(self, function_name: str) -> None:
        """
        Handle an invoked function.

        This sets the function status to `RUNNING`.

        Args:
            function_name: The name of the function to handle.
        """
        self._reset_timer()
        self._set_status(function_name, FunctionStatus.RUNNING)
        self.logger.info(f"Function {function_name} running")

    def _handle_completed(self, function_name: str) -> None:
        """
        Handle a completed function.

        This sets the function status to `COMPLETED`.

        Args:
            function_name: The name of the function to handle.
        """
        self._reset_timer()
        self._set_status(function_name, FunctionStatus.COMPLETED)
        self.logger.info(f"Function {function_name} completed")

    def _handle_failed(self, function_name: str) -> None:
        """
        Handle a failed function.

        This sets the function status to `FAILED`.

        Args:
            function_name: The name of the function to handle.
        """
        self._reset_timer()
        self._set_status(function_name, FunctionStatus.FAILED)
        self.logger.info(f"Function {function_name} failed")

    def _cascade_failure(self) -> None:
        """
        Cascade a failure to all not completed functions.

        This sets the function status to `SKIPPED`.

        Args:
            function_name: The name of the function to handle.
        """
        for function_name, status in self.get_function_statuses().items():
            if not has_completed(status):
                self._set_status(function_name, FunctionStatus.SKIPPED)
                self.logger.info(f"Skipping function {function_name} on failure")

    def _check_function_running(self, function_name: str) -> bool:
        """
        Check if a function is running. This returns True if a log file exists for the
        function on S3.

        Args:
            function_name: The name of the function to check.

        Returns:
            bool: True if the function is running, False otherwise.
        """
        try:
            invocation_folder = get_invocation_folder(self.faasr_payload)
            key = get_s3_path(f"{invocation_folder}/{function_name}.txt")
            return self._check_object_exists(key)

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"Error checking running status for {function_name}: {e}")
            return False

    def _check_function_failed(self, function_name: str) -> bool:
        """
        Check if a function has failed. This uses the `FunctionLogger` to check if the
        function has failed.

        Args:
            function_name: The name of the function to check.

        Returns:
            bool: True if the function has failed, False otherwise.
        """
        with suppress(KeyError):
            return self.function_logs[function_name].function_failed
        return False

    def _check_function_completed(self, function_name: str) -> bool:
        """
        Check if a function has completed. This uses the `FunctionLogger` to check if
        the function has completed.

        Args:
            function_name: The name of the function to check.

        Returns:
            bool: True if the function has completed, False otherwise.
        """
        with suppress(KeyError):
            return self.function_logs[function_name].logs_complete
        return False

    #####################
    # Thread management #
    #####################
    def shutdown(self, timeout: float = None) -> bool:
        """
        Attempt to gracefully shutdown the monitoring thread.

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
            self._set_shutdown_requested()

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

    def force_shutdown(self) -> None:
        """Force shutdown of the monitoring thread."""
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            self.logger.warning("Force shutting down monitoring and logs threads...")
            # Note: Python threads cannot be forcefully killed, but we can mark as shutdown
            self._set_shutdown_requested()
            self._set_monitoring_complete()

    def cleanup(self) -> None:
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
    def _create_faasr_payload_from_local_file(self) -> dict[str, Any]:
        workflow = super()._create_faasr_payload_from_local_file()

        # Set the InvocationID and InvocationTimestamp for monitoring
        workflow["InvocationID"] = str(uuid.uuid4())
        workflow["InvocationTimestamp"] = self.timestamp

        return workflow

    def trigger_workflow(self) -> None:
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

    ###################
    # Private helpers #
    ###################
    def _iter_ranks(self, function_name: str) -> Generator[str, None, None]:
        """
        Iterate over the ranks of a function.

        Args:
            function_name: The name of the function to iterate over.

        Yields:
            str: The rank of the function (e.g. "function(1)", "function(2)", etc.)
        """
        for rank in range(1, self.ranks[function_name] + 1):
            yield f"{function_name}({rank})"

    def _check_object_exists(self, key: str) -> bool:
        """
        Check if an object exists in S3.

        Args:
            key: The key of the object to check.

        Returns:
            bool: True if the object exists, False otherwise.

        Raises:
            ClientError: If an unexpected error occurs.
        """
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=str(key))
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise e
        return True

    def _check_invocation_status(self, function_name: str) -> InvocationStatus:
        """
        Check if a function was invoked. This uses the reverse adjacency graph to
        navigate each of a function's invokers.

        - If any invokers invoked the function, return `INVOKED`.
        - If any invokers are pending, return `PENDING`.
        - If all invokers did not invoke the function, return `NOT_INVOKED`.

        Args:
            function_name: The name of the function to check.

        Returns:
            InvocationStatus: The invocation status of the function.
        """
        for invoker in self.reverse_adj_graph[extract_function_name(function_name)]:
            if self.ranks[invoker] > 1:
                for rank in self._iter_ranks(invoker):
                    if status := self._get_invocation_status(rank, function_name):
                        return status
            else:
                if status := self._get_invocation_status(invoker, function_name):
                    return status

        return InvocationStatus.NOT_INVOKED

    def _get_invocation_status(
        self,
        invoker: str,
        function_name: str,
    ) -> Literal[InvocationStatus.PENDING, InvocationStatus.INVOKED] | None:
        """
        Get the invocation status of a function from a given invoker. This uses the
        `FunctionLogger` to check if the function was invoked.

        Args:
            invoker: The name of the invoker to check.
            function_name: The name of the function to check.

        Returns:
            InvocationStatus: `PENDING` or `INVOKED`.
            None: If the function was not invoked.
        """
        try:
            status = self.function_logs[invoker].get_invocation_status(function_name)
            if status == InvocationStatus.INVOKED:
                return InvocationStatus.INVOKED
            if status == InvocationStatus.PENDING:
                return InvocationStatus.PENDING
        except KeyError:
            return InvocationStatus.PENDING


def main():
    """Example of using the thread-safe WorkflowRunner"""
    import argparse

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

    while not runner.monitoring_complete:
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
