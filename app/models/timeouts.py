MODEL_CONNECT_TIMEOUT = 10


def model_request_timeout(read_timeout: int) -> tuple[int, int]:
    """连接等待使用统一上限，读取等待使用调用方配置。"""
    return min(MODEL_CONNECT_TIMEOUT, read_timeout), read_timeout
