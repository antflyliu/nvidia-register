"""日志落盘配置：带时间戳、同时输出控制台与文件。

nvidia-register 原来全是 print，无落盘、无时间，事后排查「一个账号里
hCaptcha 被求解了几次、注册接口每轮返回什么」无从对账。这里提供进程级
logger：写 ``logs/nvidia-register.log``（按天滚动，保留 30 天），每条开头
带 ``%Y-%m-%d %H:%M:%S`` 时间戳，并同时镜像到控制台保持运行时可见性。

滚动策略选型（按天 midnight 而非按大小）：
- 本项目是单进程批处理，一次跑 1~N 个账号，核心价值是「按账号/按天对账」
  （见 tag v1.0.0 功能注释），天然契合按天切分——同名文件即同一天的日志。
- 按大小滚动会让「跨日长跑」同一天横跨多个文件、按天对账要拼文件，违背用途。
- backupCount=30 覆盖月级回溯窗口；高密度日志（captcha 逐格点击/断言）降
  到 DEBUG，只入文件不入控制台，避免控制台刷屏同时保留事故回溯细节。

用法：main.py 启动时 ``from logging_setup import setup_logging; setup_logging()``
（已在 main.py:54 import 后立即调用，早于 load_config，故 config.py 取
logger 时基建已就绪），之后各处 ``logging.getLogger("nvidia-register")`` 或
``from logging_setup import get_logger; log = get_logger()``。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
# 每行开头必须有时间（事故回溯定位「第几账号第几次求解」），level 加 [] 便于
# grep ``ERROR``/``WARNING``，name 标子模块（captcha/main/email…）便于过滤。
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOGGER_NAME = "nvidia-register"

_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置进程级 logger 并返回 nvidia-register logger。

    幂等：重复调用不会重复加 handler。控制台走 INFO（运行时可见性），文件走
    DEBUG（保留逐格点击/断言等细节用于事故回溯，不入控制台避免刷屏）。
    文件按天滚动，保留 30 天。
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

    # 控制台镜像：INFO 及以上，保持原有运行时可见性，DEBUG 细节不刷屏。
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)
    logger.addHandler(stream_handler)

    # 文件落盘：DEBUG 及以上（含控制台过滤掉的细节），按天滚动，文件名带入日期，
    # 保留 30 天覆盖月级回溯。dir 建不出时静默降级到只走控制台。
    try:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=LOG_DIR / "nvidia-register.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception:
        # 无落盘能力时仍保证控制台可用。
        pass

    logger.setLevel(logging.DEBUG)  # 总门控放最低，由各 handler 各自筛
    logger.propagate = False  # 避免向 root 重复输出
    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger；不传 name 返回主 logger。

    传 name（如 "captcha"、"email"）返回 ``nvidia-register.<name>`` 子 logger，
    继承主 logger 的 handler 与级别，便于按子模块过滤日志。
    """
    if not name:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger("%s.%s" % (_LOGGER_NAME, name))
