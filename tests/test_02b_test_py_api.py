from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


from tests.conftest import WorkflowHandler


def test_py_api(handler: WorkflowHandler, s3_client: S3Client):
    handler.wait_for("test_py_api")

    input1 = handler.get_s3_key("input1.txt")
    input2 = handler.get_s3_key("input2.txt")
    input3 = handler.get_s3_key("input3.txt")

    assert s3_client.head_object(Bucket=handler.bucket_name, Key=input1) is not None
    assert s3_client.head_object(Bucket=handler.bucket_name, Key=input2) is not None
    assert s3_client.head_object(Bucket=handler.bucket_name, Key=input3) is not None
