"""日志落盘配置：带时间戳、同时输出控制台与文件。

nvidia-register 原来全是 print，无落盘、无时间，事后排查「一个账号里
hCaptcha 被求解了几次、注册接口每轮返回什么」无从对账。这里提供一个
进程级 logger：写 `logs/nvidia-register-YYYYMMDD.log`（按天滚动），每条
带 `%Y-%m-%d %H:%M:%S` 时间戳，并同时镜像到控制台，保持原有运行时可见性。

用法：main.py 启动时 `from logging_setup import setup_logging; setup_logging()`，
之后各处用 `logging.getLogger("nvidia-register")` 或直接 `logging.info(...)`。
print 仍可保留（控制台即时反馈），关键决策点改用 logger 走落盘。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOGGER_NAME = "nvidia-register"

_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置进程级 logger 并返回 nvidia-register logger。

    幂等：重复调用不会重复加 handler。文件按天滚动，保留 14 天。
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # 日志目录建不出就只走控制台，绝不因日志初始化阻断注册流程。
        pass

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    # 控制台镜像：保持原有运行时可见性。
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    # 文件落盘：按天滚动，文件名带日期，便于按天对账。
    try:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=LOG_DIR / "nvidia-register.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)
    except Exception:
        # 无落盘能力时仍保证控制台可用。
        pass

    logger.setLevel(level)
    logger.propagate = False  # 避免向 root 重复输出
    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger；不传 name 返回主 logger。"""
    if not name:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger("%s.%s" % (_LOGGER_NAME, name))
