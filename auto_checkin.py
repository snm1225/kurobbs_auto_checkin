"""库街区（鸣潮）自动签到 —— 增强版。

相对 snm1225/kurobbs_auto_checkin 原版的改动：
1. 请求带超时，不会卡死；本地运行时绕开代理（本机代理会导致连不上库街区）
2. token 入参自检：空值、首尾空白、带引号、非 JWT、长度异常会直接指出问题
3. 服务端返回 220 时给出明确结论，不再混在通用异常里
4. 请求头可通过环境变量覆盖，便于不改代码就能试出可用的组合
5. devcode 默认随机 40 位（与主流可用脚本一致），可回退固定值
6. 支持多账号：TOKEN 用 | 分隔，逐个签到并汇总结果
7. 网络抖动自动重试

环境变量：
    TOKEN            必填，库街区 token，多账号用 | 分隔
    DEBUG            非空时输出详细响应日志
    KURO_SOURCE      source 头，默认 android，可试 h5
    KURO_VERSION     version 头，默认 1.0.9，可试 2.2.5
    KURO_VERSIONCODE versioncode 头，默认 1090，对应 2250
    KURO_UA          user-agent，默认 okhttp/3.10.0，可试 okhttp/3.11.0
    KURO_DEVCODE     固定 devcode，不给则每次随机
"""
import os
import random
import string
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from loguru import logger
from pydantic import BaseModel, Field

from ext_notification import send_notification

# 本机会读取 HTTP_PROXY 走代理，走代理连库街区会失败，默认绕开代理
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

REQUEST_TIMEOUT = 25
TOKEN_INVALID_CODE = 220


class Response(BaseModel):
    code: int = Field(..., alias="code", description="返回值")
    msg: str = Field(..., alias="msg", description="提示信息")
    success: Optional[bool] = Field(None, alias="success", description="token有时才有")
    data: Optional[Any] = Field(None, alias="data", description="请求成功才有")


class KurobbsClientException(Exception):
    """Custom exception for Kurobbs client errors."""
    pass


class AuthExpiredException(KurobbsClientException):
    """登录态未通过服务端校验（code 220 或 data 为空）。

    与 KurobbsClientException 分开，是为了让调用方能区分
    "该换 token / 换 source" 和 "签到本身失败"。
    """
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _random_devcode() -> str:
    if fixed := _env("KURO_DEVCODE"):
        return fixed
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(40))


def check_token(token: str) -> List[str]:
    """检查 token 常见问题，返回问题描述列表（空列表表示没问题）"""
    if not token:
        return ["TOKEN 为空。GitHub 仓库请确认 Secrets 的 Name 是 TOKEN、Value 已填写；本地运行请设置 TOKEN 环境变量"]
    problems: List[str] = []
    if token != token.strip():
        problems.append("token 首尾含空格或换行，复制时带进了空白字符")
    if token.startswith('"') or token.startswith("'"):
        problems.append("token 首尾带了引号，复制时把引号一起带进来了")
    if not token.startswith("eyJ"):
        problems.append("token 不是 eyJ 开头的 JWT，可能抓错了字段（要抓请求头里的 Token，不是 Cookie 或响应体）")
    if len(token) < 50:
        problems.append(f"token 长度只有 {len(token)} 位，明显被截断")
    return problems


class KurobbsClient:
    FIND_ROLE_LIST_API_URL = "https://api.kurobbs.com/gamer/role/default"
    SIGN_URL = "https://api.kurobbs.com/encourage/signIn/v2"
    USER_SIGN_URL = "https://api.kurobbs.com/user/signIn"
    USER_MINE_URL = "https://api.kurobbs.com/user/mineV2"

    #: 源切换的候选顺序。库街区只认这两个值。
    SOURCE_ALTERNATIVES = ["android", "h5"]

    def __init__(self, token: str, source: Optional[str] = None):
        self.token = token
        self.source = (source or _env("KURO_SOURCE", "android") or "android").strip()
        self.result: Dict[str, str] = {}
        self.exceptions: List[Exception] = []

    def get_headers(self) -> Dict[str, str]:
        """Get the headers required for API requests.

        所有客户端标识都可通过环境变量覆盖，便于排查"换哪种头能通"。
        """
        return {
            "osversion": _env("KURO_OSVERSION", "Android"),
            "devcode": _random_devcode(),
            "countrycode": "CN",
            "ip": "10.0.2.233",
            "model": "2211133C",
            "source": self.source,
            "lang": "zh-Hans",
            "version": _env("KURO_VERSION", "1.0.9"),
            "versioncode": _env("KURO_VERSIONCODE", "1090"),
            "token": self.token,
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "accept-encoding": "gzip",
            "user-agent": _env("KURO_UA", "okhttp/3.10.0"),
        }

    def make_request(self, url: str, data: Dict[str, Any], retry: int = 3) -> Response:
        """Make a POST request to the API, with timeout and retry on network errors."""
        headers = self.get_headers()
        last_error: Optional[Exception] = None
        for attempt in range(retry):
            try:
                resp = requests.post(url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
                res = Response.model_validate_json(resp.content)
                if os.getenv("DEBUG"):
                    logger.debug(f"请求 {url} 响应：{res.model_dump_json(exclude={'data'})}")
                return res
            except Exception as e:  # 网络类异常才重试，解析类异常直接抛出
                last_error = e
                logger.warning(f"请求 {url} 第 {attempt + 1}/{retry} 次失败：{type(e).__name__}: {e}")
                time.sleep(5)
        raise KurobbsClientException(f"网络请求反复失败（{url}）：{last_error}")

    def get_mine_info(self, type: int = 1):
        """Get mine info"""
        data = {"type": type}
        res = self.make_request(self.USER_MINE_URL, data)
        return res.data

    def get_user_game_list(self, user_id: int) -> List[Dict[str, Any]]:
        """Get the list of games for the user."""
        data = {"queryUserId": user_id}
        res = self.make_request(self.FIND_ROLE_LIST_API_URL, data)
        return res.data

    def _check_auth(self, res: Response, action: str):
        """220 是库街区对无效登录态的统一返回，这里给出可直接执行的结论。"""
        if res.code == TOKEN_INVALID_CODE:
            raise AuthExpiredException(
                f"{action}失败：服务端返回 code {res.code}「{res.msg}」。"
                f"这是 token 未通过服务端校验，通常是已过期或格式不对，需要重新抓包更新 TOKEN。"
            )

    def checkin(self) -> Response:
        """Perform the check-in operation."""
        mine_info = self.get_mine_info()
        self._check_auth_on_data(mine_info, "获取个人信息")
        user_game_list = self.get_user_game_list(user_id=mine_info.get("mine", {}).get("userId", 0))
        self._check_auth_on_data(user_game_list, "获取角色列表")
        # 获取北京时间（UTC+8）
        beijing_tz = ZoneInfo('Asia/Shanghai')
        beijing_time = datetime.now(beijing_tz)

        role_info = (user_game_list.get("defaultRoleList") or [{}])[0]

        data = {
            "gameId": role_info.get("gameId", 2),
            "serverId": role_info.get("serverId", None),
            "roleId": role_info.get("roleId", 0),
            "userId": role_info.get("userId", 0),
            "reqMonth": f"{beijing_time.month:02d}",
        }
        res = self.make_request(self.SIGN_URL, data)
        self._check_auth(res, "奖励签到")
        return res

    def checkin_with_fallback(self) -> Response:
        """先按当前 source 签到，判 220 则自动换另一个 source 重试。

        实测结论：网页(H5)抓的 token 用 android 源会被服务端判 220，
        反过来安卓 App 抓的 token 用 h5 源同样失败。
        两者只差一个 header 字段，但服务端鉴权是严格区分的，
        靠人肉记忆去试很容易误判成"token 过期"而反复重新抓包。
        这里自动切一次，不用手工设 KURO_SOURCE。
        """
        tried = [self.source]
        for source in [self.source] + [s for s in self.SOURCE_ALTERNATIVES if s != self.source]:
            self.source = source
            try:
                logger.info(f"使用 source={source} 请求签到接口")
                return self.checkin()
            except AuthExpiredException as e:
                logger.warning(f"source={source} 未通过鉴权：{e}")
                continue
        raise AuthExpiredException(
            f"source 已依次试过 {'、'.join(tried)}，均返回 220。「登录已过期」"
            f"这时才基本可确认 token 本身失效，需要重新抓包。"
        )

    def sign_in(self) -> Response:
        """Perform the sign-in operation."""
        res = self.make_request(self.USER_SIGN_URL, {"gameId": 2})
        self._check_auth(res, "社区签到")
        return res

    def _check_auth_on_data(self, data: Any, action: str):
        """部分接口鉴权失败时仍返回 200 但 data 为空，这里补一层判断。"""
        if data is None:
            raise AuthExpiredException(
                f"{action}返回空数据，多半是登录态失效，请检查 TOKEN 是否过期"
            )

    def _process_sign_action(
            self,
            action_name: str,
            action_method: Callable[[], Response],
            success_message: str,
            failure_message: str,
    ):
        """
        Handle the common logic for sign-in actions.

        :param action_name: The name of the action (used to store the result).
        :param action_method: The method to call for the sign-in action.
        :param success_message: The message to log on success.
        :param failure_message: The message to log on failure.
        """
        try:
            resp = action_method()
        except KurobbsClientException as e:
            self.exceptions.append(e)
            return
        if resp.success:
            self.result[action_name] = success_message
        else:
            self.exceptions.append(KurobbsClientException(f'{failure_message}, {resp.msg}'))

    def start(self):
        """Start the sign-in process."""
        self._process_sign_action(
            action_name="checkin",
            action_method=self.checkin_with_fallback,
            success_message="签到奖励签到成功",
            failure_message="签到奖励签到失败",
        )

        self._process_sign_action(
            action_name="sign_in",
            action_method=self.sign_in,
            success_message="社区签到成功",
            failure_message="社区签到失败",
        )

        self._log()

    @property
    def msg(self):
        return ", ".join(self.result.values()) + "!"

    def _log(self):
        """Log the results and raise exceptions if any."""
        if msg := self.msg:
            logger.info(msg)
        if self.exceptions:
            raise KurobbsClientException("; ".join(map(str, self.exceptions)))


def configure_logger(debug: bool = False):
    """Configure the logger based on the debug mode."""
    logger.remove()  # Remove default logger configuration
    log_level = "DEBUG" if debug else "INFO"
    logger.add(sys.stdout, level=log_level)


def main():
    """Main function to handle command-line arguments and start the sign-in process."""
    tokens = [t.strip() for t in (os.getenv("TOKEN") or "").split("|") if t.strip()]
    debug = os.getenv("DEBUG", False)
    configure_logger(bool(debug))

    if not tokens:
        print("=" * 60)
        print("未读到 TOKEN，签到无法继续。")
        for item in check_token(""):
            print("  -", item)
        print("=" * 60)
        sys.exit(2)

    token_problems: List[str] = []
    for t in tokens:
        token_problems.extend(check_token(t))
    if token_problems:
        print("=" * 60)
        print("TOKEN 有问题，签到无法继续：")
        for item in token_problems:
            print("  -", item)
        print("=" * 60)
        sys.exit(2)

    summaries: List[str] = []
    failures: List[str] = []
    for index, token in enumerate(tokens, start=1):
        tag = f"账号{index}" if len(tokens) > 1 else "账号"
        logger.info(f"----- 开始 {tag} -----")
        try:
            kurobbs = KurobbsClient(token)
            kurobbs.start()
            summaries.append(f"{tag}：{kurobbs.msg}")
        except KurobbsClientException as e:
            # 当天已签过（本地和云端两边撞车）不是失败。
            # 不排除的话，本地 21:30 签完、GitHub 早上 6:00 再跑会每天推一条失败通知。
            if "请勿重复签到" in str(e):
                summaries.append(f"{tag}：今日已签到，无需重复操作")
                logger.info(f"{tag}今天已经签过，跳过：{e}")
            else:
                failures.append(f"{tag}：{e}")
                logger.error(str(e), exc_info=False)
        except Exception as e:
            failures.append(f"{tag}： unexpected error {e}")
            logger.exception(f"An unexpected error occurred: {e}")

    if summaries:
        send_notification("，".join(summaries))
    if failures:
        # 失败也要推送，否则每天会以为签到成功了
        send_notification("；".join(failures))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
