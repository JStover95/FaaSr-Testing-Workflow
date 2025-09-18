import logging
import os
import re
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
from invoke_workflow import WorkflowMigrationAdapter

LOGGER_NAME = "IntegrationTestWorkflowRunner"
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
        return f"Error initializing integration tester: {self.message}"


class FunctionStatus(Enum):
    """Test execution status"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class IntegrationTestWorkflowRunner(WorkflowMigrationAdapter):
    logfile_fstr = "logs/integration_test_{timestamp}.log"
    failed_regex = re.compile(r"non-zero exit code \(\d+?\) from user function")

    def __init__(self, workflow_file_path: str, timeout: int, check_interval: int):
        """
        Initialize the integration tester.

        Args:
            workflow_file_path: Path to the FaaSr workflow JSON file
            timeout: Timeout for the test in seconds
            check_interval: Interval for checking the status of the test in seconds
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
        self.timeout = timeout
        self.check_interval = check_interval

        # Thread management
        self._status_lock = threading.Lock()
        self._monitoring_thread = None
        self._monitoring_complete = False

        # Initialize S3 client for monitoring
        self.s3_client = None
        self.bucket_name = None
        self._init_s3_client()

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
                logging.FileHandler(self.logfile_fstr.format(timestamp=self.timestamp)),
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.logger = logging.getLogger(LOGGER_NAME)

    def _init_s3_client(self):
        """Initialize S3 client for monitoring"""
        print("Initializing S3 client for monitoring")

        try:
            # Create a mock FaaSrPayload to get S3 credentials
            # In a real scenario, you'd get these from your test config
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
            print(f"Initialized S3 client for bucket: {self.bucket_name}")

        except Exception as e:
            print(f"Failed to initialize S3 client: {e}")
            raise InitializationError(f"Failed to initialize S3 client: {e}")

    def _monitor_workflow_execution(self):
        """Monitor the workflow execution"""
        print(
            f"Monitoring workflow execution for functions: {', '.join(self.function_statuses.keys())}"
        )

        start_time = time.time()

        while time.time() - start_time < self.timeout:
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
                print(f"Checking function {function_name} with status {status.value}")
                all_completed = False
                if status == FunctionStatus.PENDING and self._check_function_running(
                    function_name
                ):
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.RUNNING
                    print(f"Function {function_name} running")
                elif (
                    status == FunctionStatus.RUNNING
                    and self._check_function_completion(function_name)
                ):
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.COMPLETED
                    print(f"Function {function_name} completed")
                elif status == FunctionStatus.RUNNING and self._check_function_failed(
                    function_name
                ):
                    with self._status_lock:
                        self.function_statuses[function_name] = FunctionStatus.FAILED
                    print(f"Function {function_name} failed")
                    failed = True

            if all_completed:
                print("All functions completed")
                break

            # Cascade failed status to all pending functions
            if failed:
                with self._status_lock:
                    for function_name, status in self.function_statuses.items():
                        if status == FunctionStatus.PENDING:
                            print(f"Skipping function {function_name} on failure")
                            self.function_statuses[function_name] = (
                                FunctionStatus.SKIPPED
                            )
                break

            time.sleep(self.check_interval)

        # Check for timeouts
        with self._status_lock:
            for function_name, status in self.function_statuses.items():
                if status == FunctionStatus.PENDING or status == FunctionStatus.RUNNING:
                    self.function_statuses[function_name] = FunctionStatus.TIMEOUT
                    self.logger.warning(f"Function {function_name} timed out")

        # Mark monitoring as complete
        with self._status_lock:
            self._monitoring_complete = True

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

    def _check_function_running(self, function_name: str) -> bool:
        """Check if a function is running by looking for log files in S3"""
        try:
            # Get the invocation folder path
            invocation_folder = get_invocation_folder(self.faasr_payload)

            # Check for log files
            key = f"{invocation_folder}/{function_name}.txt"
            print(f"Checking log file path: {key}")

            try:
                self.s3_client.head_object(
                    Bucket=self.bucket_name,
                    Key=str(key),
                )
                print(f"Log file found for {function_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    print(f"Log file not found for {function_name}")
                    return False
                else:
                    raise e

            return True

        except Exception as e:
            traceback.print_exc()
            print(f"Error checking running status for {function_name}: {e}")
            return False

    def _check_function_completion(self, function_name: str) -> bool:
        """Check if a function has completed by looking for .done file in S3"""
        try:
            # Get the invocation folder path
            invocation_folder = get_invocation_folder(self.faasr_payload)

            # Check for .done file
            key = f"{invocation_folder}/function_completions/{function_name}.done"
            print(f"Checking done file path: {key}")

            # List objects with this prefix
            try:
                response = self.s3_client.head_object(
                    Bucket=self.bucket_name,
                    Key=str(key),
                )
                print(f"Response: {response}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    print(f"Done file not found for {function_name}")
                    return False
                else:
                    raise e

            return True

        except Exception as e:
            traceback.print_exc()
            print(f"Error checking completion for {function_name}: {e}")
            return False

    def _check_function_failed(self, function_name: str) -> bool:
        try:
            invocation_folder = get_invocation_folder(self.faasr_payload)

            log_file_path = f"{invocation_folder}/{function_name}.txt"
            print(f"Checking log file path: {log_file_path}")
            body = None

            try:
                log_content = self.s3_client.get_object(
                    Bucket=self.bucket_name,
                    Key=str(log_file_path),
                )
                body = log_content["Body"].read().decode("utf-8")
                print(f"Log content: {body}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    print(f"Log file not found for {function_name}")
                    return False
                else:
                    raise e

            if body is not None and self.failed_regex.search(body):
                print(f"Function {function_name} failed")
                return True
            else:
                return False

        except Exception as e:
            traceback.print_exc()
            print(f"Error checking failed status for {function_name}: {e}")
            return False

    def _set_function_status(self, function_name: str, status: FunctionStatus):
        """Set the status of a function (thread-safe)"""
        with self._status_lock:
            self.function_statuses[function_name] = status

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
    """Example of using the thread-safe IntegrationTestWorkflowRunner"""

    from dotenv import load_dotenv

    load_dotenv()

    # Initialize the workflow runner
    runner = IntegrationTestWorkflowRunner(
        workflow_file_path="workflows/main.json",
        timeout=1800,  # 30 minutes
        check_interval=5,  # Check every 5 seconds
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
        print(f"📊 Current status: {current_statuses}")

        # Check for changes
        for function_name, status in current_statuses.items():
            if (
                function_name not in previous_statuses
                or previous_statuses[function_name] != status
            ):
                print(
                    f"🔄 {function_name}: {previous_statuses.get(function_name, 'UNKNOWN')} → {status.value}"
                )

        previous_statuses = current_statuses.copy()

        # Wait before next check
        time.sleep(2)

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
