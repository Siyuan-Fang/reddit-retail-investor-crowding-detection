"""HTTP 层：统一走 curl 子进程，并处理 Arctic Shift 的限流。

为什么用 curl 而不是 requests/urllib：
macOS 自带 Python 常缺根证书（urllib 会抛 CERTIFICATE_VERIFY_FAILED），
而 curl 用系统证书库，零依赖且更稳。

Arctic Shift 的限流表现（实测）：
  - 429 带 X-RateLimit-Reset 头，按头部等待
  - 422 + {"error": "Timeout. Maybe slow down a bit"} —— 这是服务端查询超时，
    不是客户端问题，必须退避重试，且往往要缩小查询窗口才能过
  - 响应体过大时直接 Connection reset by peer
"""

import json
import random
import subprocess
import time

UA = "wsb-mention-counter/1.0 (research script)"


class RateLimited(Exception):
    """服务端要求放慢；payload 太大或查询太重时也会走这里。"""


class FetchError(Exception):
    pass


def http_get(url: str, timeout: int = 90, retries: int = 5) -> str:
    """GET 并返回文本，失败按指数退避重试。"""
    last = None
    for attempt in range(retries):
        proc = subprocess.run(
            ["curl", "-sS", "-m", str(timeout), "-A", UA, "-L", url],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        last = proc.stderr.strip() or f"curl exit {proc.returncode}"
        time.sleep(min(2 ** attempt + random.random(), 30))
    raise FetchError(f"{url} 请求失败: {last}")


def get_json(url: str, timeout: int = 90, retries: int = 3) -> dict:
    """GET 并解析 JSON，识别 Arctic Shift 的限流语义。

    Arctic Shift 正常返回 {"data": [...]}；出错时 data 为 null 且带 error 字段。

    重试次数刻意压得低：服务端超时往往是因为查询窗口太宽，与其在这里耗满
    退避，不如早点抛 RateLimited 让调用方缩小窗口重试，那样快得多。
    """
    delay = 2.0
    last_err = None
    for attempt in range(retries):
        proc = subprocess.run(
            ["curl", "-sS", "-m", str(timeout), "-A", UA, "-L",
             "-w", "\n__HTTP__%{http_code}", url],
            capture_output=True, text=True,
        )
        body = proc.stdout
        code = None
        if "__HTTP__" in body:
            body, _, tail = body.rpartition("\n__HTTP__")
            code = tail.strip()

        if proc.returncode != 0:
            last_err = proc.stderr.strip() or f"curl exit {proc.returncode}"
            conn_broken = ("reset by peer" in last_err
                           or "SSL_ERROR_SYSCALL" in last_err)
            # 连接被重置可能只是瞬时 TLS 抖动，也可能是响应体过大。
            # 先按普通错误重试，只有反复失败才判定为「窗口太重」让调用方缩小。
            if conn_broken and attempt >= 2:
                raise RateLimited(f"连接反复被重置（响应可能过大）: {last_err}")
            time.sleep(delay + random.random())
            delay = min(delay * 2, 60)
            continue

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            last_err = f"HTTP {code} 返回非 JSON: {body[:120]}"
            time.sleep(delay + random.random())
            delay = min(delay * 2, 60)
            continue

        if data.get("data") is not None:
            return data

        err = (data.get("error") or "").lower()
        last_err = data.get("error") or f"HTTP {code}"
        if "timeout" in err or "slow down" in err or code == "429":
            # 服务端过载，退避后重试
            time.sleep(delay + random.random() * 2)
            delay = min(delay * 2, 60)
            continue
        raise FetchError(f"接口报错: {data.get('error')}")

    raise RateLimited(f"重试 {retries} 次仍失败: {last_err}")


def reddit_available() -> bool:
    """探测 Reddit 公开 .json 端点是否可用。

    Reddit 对机房/VPN 出口 IP 返回 403（"network security"），
    家宽 IP 通常可用。可用时能作为实时补充通道。
    """
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}",
             "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36",
             "https://www.reddit.com/r/wallstreetbets/new.json?limit=1"],
            capture_output=True, text=True, timeout=20,
        )
        return proc.stdout.strip() == "200"
    except Exception:
        return False
