def extract_function_name(function_name: str) -> str:
    return function_name.split("(")[0]


def get_s3_path(key: str) -> str:
    return key.replace("\\", "/")
