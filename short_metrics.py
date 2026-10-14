#!/usr/bin/env python3
"""给 wsb_mentions 产出的 CSV 补上做空/借券/波动率指标，生成一份新的宽表。

做空与借券（Finviz quote 页 + IBKR 官方券源文件）：
    price                最新股价，与 Finviz 快照同为收盘口径
    short_float_pct      Short Interest % of Float
    cost_to_borrow_pct   Cost to Borrow（年化费率 %）
    free_float_shares    Free Float（自由流通股数）

波动率（AlphaQuery，单位统一成年化百分数）：
    iv_30d_tenor         到期约 30 天的期权的平均隐含波动率，市场对未来 30 天的预期
    iv_pctile_3m         当前 IV 在最近约 3 个月 IV 序列里的百分位
    hv_20d_lookback      回看 20 个交易日算出的已实现波动率，偏近期
    hv_180d_lookback     回看 180 个交易日算出的已实现波动率，偏长期
    iv_skew_30d          低行权价 IV 减高行权价 IV，行权价维度的波动率斜率
    iv_skew_pctile_3m    当前 skew 在最近约 3 个月序列里的百分位

iv_skew_30d 的符号很关键：
    正值 = 低行权价期权更贵，常规的防御性 put skew（SPY、NVDA、MSFT 都在 +0.02~+0.05）
    负值 = 高行权价期权更贵，call 侧被买高，典型的博弹性 / 逼空定价
        （GME -0.148、APLD -0.094、HTZ -0.062）
和 short_float_pct、cost_to_borrow_pct 交叉看，能筛出「高空头占比 + 高借券成本
+ 负 skew」这种组合。注意它和 put/call IV 比值不是一回事，后者只反映 ATM 两侧。

注意 IV 和 HV 的天数不是一个意思，所以列名后缀特意写成 tenor / lookback：
IV 的 30 天是期权的剩余期限，朝前看；HV 的 20/180 天是回看窗口，朝后看。
AlphaQuery 的接口对两者共用 perType 参数，但语义不同，别被参数名骗了。

IBKR 的 usa.txt 是官方免认证的券源文件（每日更新，含 FEERATE/AVAILABLE），
一次下载覆盖全部代码，本地缓存 6 小时。Finviz 和 AlphaQuery 都要逐个代码请求，
每个代码 4 次 HTTP，100 个代码按默认节奏大约 5 分钟。

关于 iv_pctile_3m 的口径：AlphaQuery 的免费接口每次只返回约 64 个交易日的序列，
再长的历史要订阅，所以这里的百分位窗口是 3 个月而不是常见的 1 年，
跨代码横向比较没问题，但不要当成 IV Rank 的年度口径用。

用法：
    python3 short_metrics.py                                  # 自动挑最新的 wsb_mentions_*.csv
    python3 short_metrics.py -i wsb_mentions_7d_20261011_0953.csv
    python3 short_metrics.py --pace 2.0                       # 被限流时放慢
    python3 short_metrics.py --refresh-borrow                 # 强制重下 IBKR 券源文件
    python3 short_metrics.py --no-vol                         # 不要波动率列，只跑做空指标
"""

import argparse
import csv
import re
import sys
import time
import urllib.request
from html import unescape
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# IBKR 的券源文件，ftp2/ftp3 是同一份数据的两个镜像，挨个试。
IBKR_URLS = [
    "ftp://shortstock:@ftp2.interactivebrokers.com/usa.txt",
    "ftp://shortstock:@ftp3.interactivebrokers.com/usa.txt",
]
IBKR_CACHE = DATA / "ibkr_shortstock_usa.txt"
IBKR_TTL = 6 * 3600

FINVIZ_URL = "https://finviz.com/quote.ashx?t={}"
FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Finviz 的快照表是 label/content 成对出现的 div，按文档顺序一一对应。
SNAPSHOT_PAIR = re.compile(
    r'snapshot-td-label"[^>]*>(.*?)</div>.*?snapshot-td-content"[^>]*>(.*?)</div>',
    re.S)

ALPHAQUERY_URL = "https://www.alphaquery.com/data/option-statistic-chart"
ALPHAQUERY_HEADERS = {
    "User-Agent": FINVIZ_HEADERS["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.alphaquery.com/",
}

# (列名, perType, identifier, 百分位列名或 None)。AlphaQuery 的 perType 上限是 180-Day。
# iv-mean 的 perType 表示期权剩余期限，historical-volatility 的表示回看窗口。
# 百分位复用同一条序列算，不额外发请求。
VOL_SERIES = [
    ("iv_30d_tenor", "30-Day", "iv-mean", "iv_pctile_3m"),
    ("hv_20d_lookback", "20-Day", "historical-volatility", None),
    ("hv_180d_lookback", "180-Day", "historical-volatility", None),
    ("iv_skew_30d", "30-Day", "iv-mean-skew", "iv_skew_pctile_3m"),
]

# 追加到原表后面的新列。
NEW_COLUMNS = [
    "price",
    "short_float_pct",
    "cost_to_borrow_pct",
    "free_float_shares",
    "short_ratio",
    "borrow_available_shares",
    "iv_30d_tenor",
    "iv_pctile_3m",
    "hv_20d_lookback",
    "hv_180d_lookback",
    "iv_skew_30d",
    "iv_skew_pctile_3m",
]

SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_number(s):
    """'1.13B' -> 1130000000.0；'-'、''、None -> None。"""
    if not s:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    if s in ("-", "—", "N/A"):
        return None
    mult = 1.0
    if s and s[-1].upper() in SUFFIX:
        mult = SUFFIX[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def fmt_shares(v, capped: bool = False):
    """股数输出成 1.13b / 272.76m / 100k 这样的紧凑写法，便于肉眼横向比较。

    capped=True 时前面加 '>'，对应 IBKR 把超过上限的可借量写成 '>10000000'。
    """
    if v is None:
        return ""
    prefix = ">" if capped else ""
    for unit, scale in (("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(v) >= scale:
            # 3 位有效数字够看了，再多反而影响对齐阅读。
            return prefix + f"{v / scale:.2f}".rstrip("0").rstrip(".") + unit
    return prefix + f"{v:.0f}"


def fmt_pct(v, nd: int = 4):
    return "" if v is None else f"{v:.{nd}f}".rstrip("0").rstrip(".")


def fmt_price(v):
    """价格固定两位小数，不去尾零，看起来才像报价。"""
    return "" if v is None else f"{v:.2f}"


# ---------------------------------------------------------------- IBKR 借券费率

def load_borrow(refresh: bool = False) -> dict:
    """返回 {symbol: {"fee": 费率%, "available": 可借股数或 None, "asof": 日期}}。"""
    DATA.mkdir(exist_ok=True)
    fresh = (IBKR_CACHE.exists()
             and time.time() - IBKR_CACHE.stat().st_mtime < IBKR_TTL)

    if refresh or not fresh:
        raw = None
        for url in IBKR_URLS:
            try:
                print(f"  下载券源文件 {url.split('@')[-1]} ...")
                with urllib.request.urlopen(url, timeout=90) as r:
                    raw = r.read()
                break
            except Exception as e:  # ftp 偶尔抽风，换镜像再试
                print(f"    失败: {e}")
        if raw:
            IBKR_CACHE.write_bytes(raw)
        elif IBKR_CACHE.exists():
            print("    全部镜像失败，回退到本地旧缓存")
        else:
            print("    全部镜像失败且无缓存，Cost to Borrow 列将为空", file=sys.stderr)
            return {}
    else:
        age = int((time.time() - IBKR_CACHE.stat().st_mtime) / 60)
        print(f"  使用本地券源缓存（{age} 分钟前）")

    text = IBKR_CACHE.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    asof = ""
    if lines and lines[0].startswith("#BOF"):
        parts = lines[0].split("|")
        if len(parts) >= 3:
            asof = f"{parts[1]} {parts[2]}"

    out = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        f = line.split("|")
        if len(f) < 8:
            continue
        sym = f[0].strip().upper()
        fee = parse_number(f[6])
        if fee is None:
            continue
        # AVAILABLE 可能是 ">10000000" 这种上限写法，剥掉 '>' 再转数字，
        # 但要记住它是个下界，不然 ">10m" 会被当成精确值。
        raw_avail = f[7].strip()
        capped = raw_avail.startswith(">")
        avail = parse_number(raw_avail.lstrip(">").strip())
        out[sym] = {"fee": fee, "available": avail,
                    "capped": capped, "asof": asof}

    print(f"  券源文件 {len(out)} 个代码，数据日期 {asof or '未知'}")
    return out


# ------------------------------------------------------------------ Finviz 快照

def fetch_finviz(session: requests.Session, ticker: str, retries: int = 3):
    """抓一个代码的快照表，返回 {label: value}；失败返回 None。"""
    url = FINVIZ_URL.format(ticker)
    for attempt in range(retries):
        try:
            r = session.get(url, headers=FINVIZ_HEADERS, timeout=25)
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"    {ticker}: 请求异常 {e}")
                return None
            time.sleep(3 * (attempt + 1))
            continue

        if r.status_code == 404:
            return {}  # Finviz 没收录（多为非美股或已退市），不算错误
        if r.status_code in (429, 403) or r.status_code >= 500:
            wait = 10 * (attempt + 1)
            print(f"    {ticker}: HTTP {r.status_code}，{wait}s 后重试")
            time.sleep(wait)
            continue

        pairs = SNAPSHOT_PAIR.findall(r.text)
        return {strip_tags(a): strip_tags(b) for a, b in pairs}

    print(f"    {ticker}: 重试耗尽，留空")
    return None


# -------------------------------------------------------------- AlphaQuery 波动率

def fetch_series(session: requests.Session, ticker: str, per_type: str,
                 identifier: str, retries: int = 3):
    """取一条波动率时间序列，返回按时间升序的 float 列表（已剔除空值）。"""
    params = {"ticker": ticker, "perType": per_type, "identifier": identifier}
    for attempt in range(retries):
        try:
            r = session.get(ALPHAQUERY_URL, params=params,
                            headers=ALPHAQUERY_HEADERS, timeout=25)
        except requests.RequestException:
            if attempt == retries - 1:
                return []
            time.sleep(3 * (attempt + 1))
            continue

        if r.status_code == 404:
            return []  # 这个代码 AlphaQuery 没有数据
        if r.status_code in (429, 403) or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue

        try:
            data = r.json()
        except ValueError:
            return []
        # 原始数据是 [{"x": ISO 日期, "value": 0.4555 或 null}, ...]，
        # value 是小数形式的年化波动率，这里保持原样，写出时再乘 100。
        return [p["value"] for p in data
                if isinstance(p, dict) and isinstance(p.get("value"), (int, float))]

    return []


def percentile_of(series, value):
    """value 在 series 中的百分位（0~100），用「不高于它的样本占比」口径。"""
    if not series or value is None:
        return None
    below = sum(1 for v in series if v <= value)
    return 100.0 * below / len(series)


def fetch_vol(session: requests.Session, ticker: str, pace: float) -> dict:
    """抓一个代码的全部波动率指标，返回已格式化好的列值。"""
    out = {}
    for col, per_type, identifier, pctile_col in VOL_SERIES:
        series = fetch_series(session, ticker, per_type, identifier)
        time.sleep(pace)
        if not series:
            continue
        latest = series[-1]
        # 原始值是小数，统一乘 100 变成波动率点数。skew 是两个 IV 之差，
        # 乘 100 之后和 IV/HV 同量纲，可以直接放在一起看。
        out[col] = fmt_pct(latest * 100, 2)
        if pctile_col:
            out[pctile_col] = fmt_pct(percentile_of(series, latest), 1)
    return out


# ------------------------------------------------------------------------ 主流程

def pick_input() -> Path:
    files = sorted(ROOT.glob("wsb_mentions_*.csv"))
    if not files:
        raise SystemExit("当前目录没有 wsb_mentions_*.csv，请用 -i 指定输入文件")
    return max(files, key=lambda p: p.stat().st_mtime)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", help="输入 CSV，默认取最新的 wsb_mentions_*.csv")
    ap.add_argument("-o", "--output", help="输出 CSV，默认在输入文件名后加 _short")
    ap.add_argument("--pace", type=float, default=0.8, help="每次 HTTP 请求的间隔秒数，默认 0.8")
    ap.add_argument("--refresh-borrow", action="store_true", help="强制重下 IBKR 券源文件")
    ap.add_argument("--skip-etf", action="store_true",
                    help="跳过 ETF 的 Finviz 抓取（ETF 页面本来就没有这几项）")
    ap.add_argument("--no-vol", action="store_true", help="不抓波动率，只跑做空指标")
    args = ap.parse_args()

    src = Path(args.input).expanduser() if args.input else pick_input()
    if not src.is_absolute():
        src = (ROOT / src).resolve()
    if not src.exists():
        raise SystemExit(f"找不到输入文件: {src}")

    dst = Path(args.output) if args.output else src.with_name(src.stem + "_short.csv")
    if not dst.is_absolute():
        dst = (ROOT / dst).resolve()

    # wsb_mentions.py 写的是 utf-8-sig，这里用同样的编码读写，Excel 打开不乱码。
    with open(src, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        base_cols = list(reader.fieldnames or [])

    if "ticker" not in base_cols:
        raise SystemExit(f"输入文件没有 ticker 列：{base_cols}")

    print(f"输入: {src.name}（{len(rows)} 行）")

    print("加载 IBKR 借券费率...")
    borrow = load_borrow(refresh=args.refresh_borrow)

    per_ticker = 1 + (0 if args.no_vol else len(VOL_SERIES))
    print(f"逐个代码抓取（{per_ticker} 次请求/个，间隔 {args.pace}s）...")
    session = requests.Session()
    hit = miss = 0

    for n, row in enumerate(rows, 1):
        ticker = (row.get("ticker") or "").strip().upper()
        for c in NEW_COLUMNS:
            row[c] = ""
        if not ticker:
            continue

        is_etf = str(row.get("is_etf", "")).strip().lower() in ("true", "1", "yes")

        snap = {}
        if not (args.skip_etf and is_etf):
            snap = fetch_finviz(session, ticker) or {}
            time.sleep(args.pace)

        row["price"] = fmt_price(parse_number(snap.get("Price")))
        short_float = parse_number(snap.get("Short Float"))
        free_float = parse_number(snap.get("Shs Float"))
        row["short_float_pct"] = fmt_pct(short_float)
        row["free_float_shares"] = fmt_shares(free_float)
        row["short_ratio"] = fmt_pct(parse_number(snap.get("Short Ratio")))

        b = borrow.get(ticker)
        if b:
            row["cost_to_borrow_pct"] = fmt_pct(b["fee"])
            row["borrow_available_shares"] = fmt_shares(b["available"], b["capped"])

        vol = {} if args.no_vol else fetch_vol(session, ticker, args.pace)
        row.update(vol)

        if row["price"] or short_float is not None or b or vol:
            hit += 1
        else:
            miss += 1

        if n % 10 == 0 or n == len(rows):
            print(f"  {n}/{len(rows)}  命中 {hit} 缺失 {miss}")

    columns = base_cols + [c for c in NEW_COLUMNS if c not in base_cols]
    with open(dst, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"\n已保存 {len(rows)} 行 -> {dst.name}")
    if borrow:
        asof = next(iter(borrow.values()))["asof"]
        print(f"借券费率数据日期: {asof}")

    head = (f"{'代码':<7} {'价格':>9} {'SI%Float':>9} {'CTB%':>8} "
            f"{'FreeFloat':>14}")
    if not args.no_vol:
        head += (f" {'IV@30d':>7} {'IV%ile':>7} {'HV<20d':>7} {'HV<180d':>7}"
                 f" {'Skew':>7} {'Sk%ile':>7}")
    print(f"\n{head}")
    for r in rows[:20]:
        line = (f"{r['ticker']:<7} {r['price'] or '-':>9} "
                f"{r['short_float_pct'] or '-':>9} "
                f"{r['cost_to_borrow_pct'] or '-':>8} "
                f"{r['free_float_shares'] or '-':>14}")
        if not args.no_vol:
            line += (f" {r['iv_30d_tenor'] or '-':>7} {r['iv_pctile_3m'] or '-':>7} "
                     f"{r['hv_20d_lookback'] or '-':>7} {r['hv_180d_lookback'] or '-':>7} "
                     f"{r['iv_skew_30d'] or '-':>7} {r['iv_skew_pctile_3m'] or '-':>7}")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
