import json
import logging

logger = logging.getLogger(__name__)

# 插件模式下，CONFIG 由 Provider.__init__ 外部注入，不读写文件
CONFIG = {}


def load_config():
    """返回当前 CONFIG（由外部注入）"""
    return CONFIG


def save_config(cfg):
    """插件模式下不持久化到文件，仅在内存中更新"""
    global CONFIG
    CONFIG = cfg
