from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

import logging
import re
import threading
import time
from enum import Enum

from scripts.utils import extract_function_name, get_s3_path


class InvocationStatus(Enum):
    PENDING = "pending"
    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"


class FunctionLogger:
    failed_regex = re.compile(r"\[[\d\.]+?\] \[ERROR\]")
    invoked_regex = re.compile(
        r"(?<=\[scheduler.py\] GitHub Action: Successfully invoked: )[a-zA-Z][a-zA-Z0-9\-]+"
    )

    def __init__(
        self,
        *,
        function_name: str,
        workflow_name: str,
        invocation_folder: str,
        bucket_name: str,
        s3_client: S3Client,
        stream_logs: bool = False,
        interval_seconds: int = 3,
    ):
        self.function_name = function_name
        self.workflow_name = workflow_name
        self.invocation_folder = invocation_folder
        self.bucket_name = bucket_name
        self.s3_client = s3_client
        self.stream_logs = stream_logs
        self.interval_seconds = interval_seconds

        # Setup logger
        self.logger_name = f"FunctionLogger-{function_name}"
        self.logger = self._setup_logger()
        self._logs: list[str] = []

        # Initialize statuses
        self._function_complete = False
        self._logs_complete = False
        self._function_failed = False
        self._invocations: set[str] | None = None

        # Thread management
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _setup_logger(self) -> logging.Logger:
        """
        Initialize the FunctionLogger logger. Log outputs include the function name.

        Returns:
            logging.Logger: The logger for the FunctionLogger.
        """
        logger = logging.getLogger(self.logger_name)
        handler = logging.StreamHandler()
        formatter = logging.Formatter(f"[{self.logger_name}] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    @property
    def logs(self) -> list[str]:
        """
        Get the logs as a list (thread-safe).

        Returns:
            list[str]: The logs.
        """
        with self._lock:
            return self._logs

    @property
    def logs_content(self) -> str:
        """
        Get the logs as a string (thread-safe).

        Returns:
            str: The logs content.
        """
        with self._lock:
            return "\n".join(self._logs)

    @property
    def function_complete(self) -> bool:
        """
        Get the function complete flag (thread-safe).

        This is True when the function's `.done` file exists on S3.

        Returns:
            bool: The function complete flag.
        """
        with self._lock:
            return self._function_complete

    @property
    def logs_complete(self) -> bool:
        """
        Get the logs complete flag (thread-safe).

        This is True when the `.done` file exists and no new logs were fetched after a
        monitoring cycle.

        Returns:
            bool: The logs complete flag.
        """
        with self._lock:
            return self._logs_complete

    @property
    def function_failed(self) -> bool:
        """
        Get the function failed flag (thread-safe).

        This is True when an error-level log is found. This will return True even if
        the error is logged after the `.done` file exists.

        Returns:
            bool: The function failed flag.
        """
        with self._lock:
            return self._function_failed

    @property
    def invocations(self) -> set[str] | None:
        """
        Get the invocations (thread-safe).

        Returns:
            set[str] | None: The invocations.
        """
        with self._lock:
            if self._invocations is None:
                return None
            return self._invocations.copy()

    def _update_logs(self, new_logs: list[str]) -> None:
        """
        Update the logs (thread-safe).

        Args:
            new_logs: The new logs to add to the logs.
        """
        with self._lock:
            self._logs += new_logs

    def _set_function_complete(self) -> None:
        """Set the function complete flag to True (thread-safe)."""
        with self._lock:
            self._function_complete = True

    def _set_logs_complete(self) -> None:
        """Set the logs complete flag to True (thread-safe)."""
        with self._lock:
            self._logs_complete = True

    def _set_function_failed(self) -> None:
        """Set the function failed flag to True (thread-safe)."""
        with self._lock:
            self._function_failed = True

    def _set_invocations(self) -> None:
        """
        Set the function's invocations (thread-safe).

        This pulls the names of all invoked functions from the logs (excluding ranks).
        """
        logs_content = self.logs_content
        with self._lock:
            self._invocations = set(
                re.sub(r"^" + self.workflow_name + "-", "", invocation)
                for invocation in self.invoked_regex.findall(logs_content)
            )

    @property
    def key(self) -> str:
        """
        Get the complete logs S3 key.

        Returns:
            str: The S3 key for the logs.
        """
        return get_s3_path(f"{self.invocation_folder}/{self.function_name}.txt")

    @property
    def done_key(self) -> str:
        """
        Get the complete `.done` file S3 key.

        This replaces ranks with the expected format
        (e.g. "function(1)" -> "function.1.done").

        Returns:
            str: The S3 key for the done file.
        """
        s3_function_name = self.function_name.replace("(", ".").replace(")", "")
        return get_s3_path(
            f"{self.invocation_folder}/function_completions/{s3_function_name}.done"
        )

    def get_invocation_status(self, function_name: str) -> InvocationStatus:
        """
        Get the invocation status of a function invoked by this function.

        - If invocations have not been registered, return `PENDING`.
        - If the function was invoked, return `INVOKED`.
        - Otherwise, return `NOT_INVOKED`.

        Args:
            function_name: The name of the function to get the invocation status of.

        Returns:
            InvocationStatus: The invocation status of the function.
        """
        if self.invocations is None:
            return InvocationStatus.PENDING
        if extract_function_name(function_name) in self.invocations:
            return InvocationStatus.INVOKED
        return InvocationStatus.NOT_INVOKED

    def start(self) -> None:
        """Start the FunctionLogger."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """
        Run the main logging loop.

        Until logs are complete, this:
        - Fetches new logs from S3.
        - Updates the logs.
        - Checks for a failure.
        - Checks for a done file.
        - Sets the logs complete flag if no new logs were fetched after a monitoring
          cycle.

        Once logs are complete, this:
        - Sets the logs complete flag.
        - Sets the invocations.

        If `stream_logs` is True, this will also log the logs to the console.
        """
        while not self.logs_complete:
            log_content = self._get_from_s3()
            num_existing_logs = len(self.logs)
            new_logs = log_content[num_existing_logs:]

            self._update_logs(new_logs)

            if self.stream_logs:
                for log in new_logs:
                    self.logger.info(log)

            if self._check_for_failure():
                self._set_function_failed()
                self._set_logs_complete()
            elif not self.function_complete and self._check_for_done():
                self._set_function_complete()
            elif self.function_complete and not new_logs:
                self._set_logs_complete()

            time.sleep(self.interval_seconds)

        self._set_invocations()

    def _check_for_done(self) -> bool:
        """
        Check if the complete `.done` file exists on S3.

        Returns:
            bool: True if the complete `.done` file exists on S3, False otherwise.

        Raises:
            ClientError: If an unexpected error occurs.
        """
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=self.done_key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise e
        return True

    def _get_from_s3(self) -> list[str]:
        """
        Get the logs from S3.

        Returns:
            list[str]: The logs.
        """
        log_content = self.s3_client.get_object(Bucket=self.bucket_name, Key=self.key)
        return log_content["Body"].read().decode("utf-8").strip().split("\n")

    def _check_for_failure(self) -> bool:
        """
        Check if the logs contain an error-level log.

        Returns:
            bool: True if the logs contain an error-level log, False otherwise.
        """
        return self.failed_regex.search(self.logs_content) is not None
