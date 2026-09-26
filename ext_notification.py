import os
from urllib.parse import quote

import requests
from loguru import logger

# serverchan_sdk 是可选依赖：没装也能跑签到，只是推不了 Server 酱
try:
    from serverchan_sdk import sc_send
    SERVERCHAN_AVAILABLE = True
except ImportError:
    SERVERCHAN_AVAILABLE = False
    sc_send = None


def send_notification(message):
    title = "库街区自动签到任务"
    send_bark_notification(title, message)
    send_server3_notification(title, message)


def send_bark_notification(title, message):
    """Send a notification via Bark."""
    bark_device_key = os.getenv("BARK_DEVICE_KEY")
    bark_server_url = os.getenv("BARK_SERVER_URL")

    if not bark_device_key or not bark_server_url:
        logger.debug("Bark secrets are not set. Skipping notification.")
        return

    # 标题和内容都要百分号编码，否则中文和 token 里的点号会破坏 URL
    url = f"{bark_server_url.rstrip('/')}/{quote(bark_device_key)}/{quote(title)}/{quote(message)}"
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        logger.warning(f"Bark 推送失败：{e}")


def send_server3_notification(title, message):
    server3_send_key = os.getenv("SERVER3_SEND_KEY")
    if server3_send_key and SERVERCHAN_AVAILABLE:
        try:
            response = sc_send(server3_send_key, title, message, {"tags": "Github Action|库街区"})
            logger.debug(response)
        except Exception as e:
            logger.warning(f"Server 酱推送失败：{e}")
    elif server3_send_key and not SERVERCHAN_AVAILABLE:
        logger.warning("已配置 SERVER3_SEND_KEY，但缺少 serverchan_sdk，跳过推送")
    else:
        logger.debug("ServerChan3 send key not exists.")
