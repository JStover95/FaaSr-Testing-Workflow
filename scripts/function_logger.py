from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

import logging
import re
import threading
import time
from enum import Enum


def extract_function_name(function_name: str) -> str:
    return function_name.split("(")[0]


class InvocationStatus(Enum):
    PENDING = "pending"
    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"


class FunctionLogger:
    failed_regex = re.compile(r"\[[\d\.]+?\] \[ERROR\]")
    invoked_regex = re.compile(
        r"(?<=\[scheduler.py\] GitHub Action: Successfully invoked: )[a-zA-Z\-_]+"
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

        self.logger_name = f"FunctionLogger-{function_name}"
        self.logger = self._setup_logger()

        self._logs: list[str] = []
        self._function_complete = False
        self._logs_complete = False

        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._function_failed = False
        self.invocations: set[str] | None = None

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(self.logger_name)
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            f"[%(levelname)s] [{self.logger_name}] %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    @property
    def logs(self) -> list[str]:
        with self._lock:
            return self._logs

    @property
    def logs_content(self) -> str:
        with self._lock:
            return "\n".join(self._logs)

    @property
    def logs_complete(self) -> bool:
        with self._lock:
            return self._logs_complete

    @property
    def function_failed(self) -> bool:
        with self._lock:
            return self._function_failed

    @property
    def key(self) -> str:
        return f"{self.invocation_folder}/{self.function_name}.txt".replace("\\", "/")

    def set_function_complete(self) -> None:
        with self._lock:
            self._function_complete = True

    def get_invocation_status(self, function_name: str) -> InvocationStatus:
        if self.invocations is None:
            return InvocationStatus.PENDING
        # Invocations do not include the rank
        if extract_function_name(function_name) in self.invocations:
            return InvocationStatus.INVOKED
        return InvocationStatus.NOT_INVOKED

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self.logs_complete:
            log_content = self._get_from_s3()
            num_existing_logs = len(self.logs)
            new_logs = log_content[num_existing_logs:]

            self._update_logs(new_logs)

            if self.stream_logs:
                for log in new_logs:
                    self.logger.info(log)

            if self._get_function_complete() and not new_logs:
                self._set_logs_complete()
            elif self._check_for_failure():
                self._set_function_failed()
                self._set_logs_complete()

            time.sleep(self.interval_seconds)

        self._set_invocations()

    def _get_from_s3(self) -> list[str]:
        log_content = self.s3_client.get_object(
            Bucket=self.bucket_name,
            Key=self.key,
        )
        return log_content["Body"].read().decode("utf-8").strip().split("\n")

    def _update_logs(self, new_logs: list[str]) -> None:
        with self._lock:
            self._logs += new_logs

    def _get_function_complete(self) -> bool:
        with self._lock:
            return self._function_complete

    def _set_logs_complete(self) -> None:
        with self._lock:
            self._logs_complete = True

    def _set_function_failed(self) -> None:
        with self._lock:
            self._function_failed = True

    def _check_for_failure(self) -> bool:
        return self.failed_regex.search(self.logs_content) is not None

    def _set_invocations(self) -> set[str]:
        self.invocations = set(
            re.sub(r"^" + self.workflow_name + "-", "", invocation)
            for invocation in self.invoked_regex.findall(self.logs_content)
        )
