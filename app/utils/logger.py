import os
import logging
from logging import StreamHandler

class FilepathLogHandler(StreamHandler):
    def format(self, record):
        record.filepath = os.path.abspath(record.pathname)
        message = super().format(record)
        return f"[{record.filepath}]\n{message}"

def setup_logger(name):
    logger = logging.getLogger(name)

    if not logger.hasHandlers():
        logger.setLevel(logging.DEBUG)

        # 设置自定义处理器
        handler = FilepathLogHandler()
        formatter = logging.Formatter(fmt='\n-----开始-----\n%(asctime)s\n%(levelname)-8s\n[%(filepath)s:%(lineno)d]\n%(message)s\n-----结束-----\n\n')
        handler.setFormatter(formatter)

        # Check if the handler is already added
        if handler not in logger.handlers:
            logger.addHandler(handler)
    
    return logger

# 使用自定义 Logger
logger = setup_logger(__name__)

# # 测试日志打印
# def test_logging():
#     logger.info("This is an info message.")
#     logger.error("This is an error message.")

# test_logging()
