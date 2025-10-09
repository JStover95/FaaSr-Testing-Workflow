from typing import Any

import boto3
from botocore.exceptions import ClientError


class S3ClientInitializationError(Exception):
    """Exception raised for S3 client initialization errors"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"Error initializing S3 client: {self.message}"


class S3ClientError(Exception):
    """Exception raised for S3 client errors"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"S3 client error: {self.message}"


class FaaSrS3Client:
    def __init__(
        self,
        *,
        workflow_data: dict[str, Any],
        access_key: str,
        secret_key: str,
    ):
        try:
            default_datastore = workflow_data.get("DefaultDataStore", "My_S3_Bucket")
            datastore_config = workflow_data["DataStores"][default_datastore]

            if datastore_config.get("Endpoint"):
                self._client = boto3.client(
                    "s3",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=datastore_config["Region"],
                    endpoint_url=datastore_config["Endpoint"],
                )
            else:
                self._client = boto3.client(
                    "s3",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=datastore_config["Region"],
                )

            self._bucket_name = datastore_config["Bucket"]

        except ClientError as e:
            raise S3ClientInitializationError(f"boto3 client error: {e}") from e
        except KeyError as e:
            raise S3ClientInitializationError(f"Key error: {e}") from e
        except Exception as e:
            raise S3ClientInitializationError(f"Unhandled error: {e}") from e

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket_name, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise S3ClientError(f"Error checking object existence: {e}") from e
        return True

    def get_object(self, key: str, encoding: str = "utf-8") -> str:
        try:
            return (
                self._client.get_object(Bucket=self._bucket_name, Key=key)["Body"]
                .read()
                .decode(encoding)
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                raise S3ClientError(f"Object does not exist: {e}") from e
            raise S3ClientError(f"boto3 client error getting object: {e}") from e
        except Exception as e:
            raise S3ClientError(f"Unhandled error getting object: {e}") from e
