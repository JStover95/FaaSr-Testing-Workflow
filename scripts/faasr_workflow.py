import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from itertools import chain
from typing import Any, Generator, Literal

from FaaSr_py.helpers.graph_functions import build_adjacency_graph
from FaaSr_py.helpers.s3_helper_functions import get_invocation_folder

from scripts.faasr_function import FaaSrFunction
from scripts.invoke_workflow import WorkflowMigrationAdapter
from scripts.s3_client import FaaSrS3Client
from scripts.utils import (
    completed,
    extract_function_name,
    failed,
    has_completed,
    has_final_state,
    invoked,
    not_invoked,
    pending,
    running,
)
from scripts.utils.enums import FunctionStatus, InvocationStatus

REQUIRED_ENV_VARS = [
    "MY_S3_BUCKET_ACCESSKEY",
    "MY_S3_BUCKET_SECRETKEY",
    "GITHUB_TOKEN",
    "GITHUB_REPOSITORY",
]


class InitializationError(Exception):
    """Exception raised for FaaSrWorkflow initialization errors"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"Error initializing FaaSrWorkflow: {self.message}"


class StopMonitoring(Exception):
    """Exception raised to stop FaaSrWorkflow monitoring"""


class FaaSrWorkflow(WorkflowMigrationAdapter):
    """
    Orchestrates workflow execution and monitoring using the new FaaSr architecture.

    This class is responsible for:
    - Triggering workflows via WorkflowMigrationAdapter
    - Orchestrating monitoring of all FaaSrFunction instances
    - Handling workflow-level status updates (invocations, cascading failures, timeouts)
    """

    logger_name = "FaaSrWorkflow"

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
        self._stream_logs = stream_logs
        self._functions: dict[str, FaaSrFunction] = {}
        self._prev_statuses: dict[str, FunctionStatus] = {}

        # Initialize S3 client for monitoring
        self.s3_client = FaaSrS3Client(
            workflow_data=self.workflow_data,
            access_key=os.getenv("MY_S3_BUCKET_ACCESSKEY"),
            secret_key=os.getenv("MY_S3_BUCKET_SECRETKEY"),
        )

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
        Initialize the FaaSrWorkflow logger.

        Returns:
            logging.Logger: The logger for the FaaSrWorkflow.
        """
        logger = logging.getLogger(self.logger_name)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(levelname)s] [%(filename)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

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
            for function in invoked_functions:
                reverse_adj_graph[function].add(invoker)
        return reverse_adj_graph

    def _build_functions(self, stream_logs: bool) -> list[FaaSrFunction]:
        """
        Initialize the function statuses:

        ```py
        {
            "function_name": FaaSrFunction,
            "ranked_function(1)": FaaSrFunction,
            "ranked_function(2)": FaaSrFunction,
            ...
        }
        ```

        - The `WorkflowInvoke` function is initially set to `INVOKED`.
        - All other functions are initially set to `PENDING`.
        - Ranked functions include the rank in the function name.

        Returns:
            list[FaaSrFunction]: The function instances.
        """
        functions: dict[str, FaaSrFunction] = {}
        for rank in chain(*(self._iter_ranks(name) for name in self.function_names)):
            function = FaaSrFunction(
                function_name=rank,
                workflow_name=self.workflow_name,
                invocation_folder=get_invocation_folder(self.faasr_payload),
                s3_client=self.s3_client,
                stream_logs=stream_logs,
            )
            if extract_function_name(rank) == self.workflow_invoke:
                function.set_status(FunctionStatus.INVOKED)
            functions[rank] = function
        return functions

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
            return {
                function.function_name: function.status
                for function in self._functions.values()
            }

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
            f"Monitoring workflow execution for functions: {', '.join(self.function_names)}"
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
        - Starts a function instance if the function has run and no instance exists.
        - Handles changes in each function status.
        - Cascades a failure to all pending functions when any function fails.

        Raises:
            StopMonitoring: If all functions have successfully completed or a failure has been detected.
        """
        workflow_failed = False

        # Check completion status for each function
        for function in self._functions.values():
            if pending(function.status):
                self._handle_pending(function)
            if self._prev_statuses[function.function_name] != function.status:
                self._log_status_change(function)
                self._prev_statuses[function.function_name] = function.status
            if failed(function.status):
                workflow_failed = True
                break

        if self._all_functions_completed():
            self.logger.info("All functions completed")
            raise StopMonitoring("All functions completed")

        # Cascade failed status to all pending functions
        if workflow_failed:
            self._cascade_failure()
            raise StopMonitoring("Failure detected")

    def _handle_pending(self, function: FaaSrFunction) -> None:
        """
        Handle a pending function.

        This sets the function status to `INVOKED` if the function was invoked,
        and `NOT_INVOKED` if the function was not invoked.

        Args:
            function_name: The name of the function to handle.
        """
        invocation_status = self._check_invocation_status(function)
        if invocation_status == InvocationStatus.INVOKED:
            self._reset_timer()
            function.set_status(FunctionStatus.INVOKED)
        elif invocation_status == InvocationStatus.NOT_INVOKED:
            self._reset_timer()
            function.set_status(FunctionStatus.NOT_INVOKED)

    def _log_status_change(self, function: FaaSrFunction) -> None:
        if failed(function.status):
            self.logger.info(f"Function {function.function_name} failed")
        elif not_invoked(function.status):
            self.logger.info(f"Function {function.function_name} not invoked")
        elif invoked(function.status):
            self.logger.info(f"Function {function.function_name} invoked")
        elif running(function.status):
            self.logger.info(f"Function {function.function_name} running")
        elif completed(function.status):
            self.logger.info(f"Function {function.function_name} completed")

    def _all_functions_completed(self) -> bool:
        """
        Check if all functions have completed.
        """
        return all(
            has_completed(function.status) for function in self._functions.values()
        )

    def _finish_monitoring(self) -> None:
        """
        Finish monitoring. This:

        - Checks for any functions that have not completed and:
           - Sets the function status to `SKIPPED` if a shutdown was requested.
           - Otherwise, sets the function status to `TIMEOUT`.
        - Marks the monitoring as complete.
        """
        # Check for timeouts or shutdown
        for function in self._functions.values():
            if not has_final_state(function.status) and self.shutdown_requested:
                function.set_status(FunctionStatus.SKIPPED)
                self.logger.info(
                    f"Function {function.function_name} skipped due to shutdown"
                )
            elif not has_final_state(function.status):
                function.set_status(FunctionStatus.TIMEOUT)
                self.logger.warning(f"Function {function.function_name} timed out")

        # Mark monitoring as complete
        self._set_monitoring_complete()

        self.logger.info("Monitoring complete")

    def _cascade_failure(self) -> None:
        """
        Cascade a failure to all not completed functions.

        This sets the function status to `SKIPPED`.

        Args:
            function: The function to handle.
        """
        for function in self._functions.values():
            if not has_completed(function.status):
                function.set_status(FunctionStatus.SKIPPED)
                self.logger.info(
                    f"Skipping function {function.function_name} on failure"
                )

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

        # Build functions after trigger_workflow initializes FaaSrPayload
        self._functions = self._build_functions(self._stream_logs)
        self._prev_statuses = self.get_function_statuses()

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
        if self.ranks[function_name] <= 1:
            yield function_name
        else:
            for rank in range(1, self.ranks[function_name] + 1):
                yield f"{function_name}({rank})"

    def _check_invocation_status(self, function: FaaSrFunction) -> InvocationStatus:
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
        for rank in chain(
            *(
                self._iter_ranks(invoker)
                for invoker in self.reverse_adj_graph[
                    extract_function_name(function.function_name)
                ]
            )
        ):
            if status := self._get_invocation_status(self._functions[rank], function):
                return status
        return InvocationStatus.NOT_INVOKED

    def _get_invocation_status(
        self,
        invoker: FaaSrFunction,
        function: FaaSrFunction,
    ) -> Literal[InvocationStatus.PENDING, InvocationStatus.INVOKED] | None:
        """
        Get the invocation status of a function from a given invoker. This uses the
        `FaaSrFunction` to check if the function was invoked.

        Args:
            invoker: The name of the invoker to check.
            function: The function to check.

        Returns:
            InvocationStatus: `PENDING` or `INVOKED`.
            None: If the function was not invoked.
        """
        # Check if the invoker has completed and has invocations
        if invoker.invocations is not None:
            invocations = invoker.invocations
            if extract_function_name(function.function_name) in invocations:
                return InvocationStatus.INVOKED
            else:
                return InvocationStatus.NOT_INVOKED
        else:
            return InvocationStatus.PENDING


def main():
    """Example of using the thread-safe FaaSrWorkflow"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-file", type=str, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--check-interval", type=int, default=1)
    parser.add_argument("--stream-logs", type=bool, default=True)
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    # Initialize the workflow
    workflow = FaaSrWorkflow(
        workflow_file_path=args.workflow_file,
        timeout=args.timeout,
        check_interval=args.check_interval,
        stream_logs=args.stream_logs,
    )

    # Start the workflow (returns immediately)
    print("🚀 Starting workflow...")
    workflow.trigger_workflow()
    print("✅ Workflow started, monitoring in background")

    # Monitor status changes
    print("\n📊 Monitoring function statuses...")
    previous_statuses = {k: None for k in workflow.get_function_statuses()}

    while not workflow.monitoring_complete:
        # Get current statuses (thread-safe)
        current_statuses = workflow.get_function_statuses()

        # Check for changes
        for function_name, status in current_statuses.items():
            if previous_statuses[function_name] != status:
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
    final_statuses = workflow.get_function_statuses()
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
