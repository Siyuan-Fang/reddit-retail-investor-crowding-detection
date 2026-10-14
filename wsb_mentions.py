#!/usr/bin/env python3
"""统计 r/wallstreetbets 里被提及最多的股票，不需要 Reddit API key。

数据来自 Arctic Shift（Pushshift 的继任者，免认证，延迟约 1 分钟），
按时间窗口全量抓取评论与帖子，本地 SQLite 缓存支持断点续抓。

用法：
    python3 wsb_mentions.py                  # 最近 1 天
    python3 wsb_mentions.py --days 7         # 最近 7 天
    python3 wsb_mentions.py --no-scrape      # 只用本地缓存重新统计
    python3 wsb_mentions.py --pace 2.0       # 服务端繁忙时放慢请求

后台跑长任务（抓 7 天要两三个小时）：
    nohup python3 wsb_mentions.py --days 7 > wsb7d.log 2>&1 &
    tail -f wsb7d.log
"""

import argparse
import csv
import sys
import time
from pathlib import Path

from wsb.extract import tally
from wsb.fetchers import reddit_available
from wsb.scrape import scrape
from wsb.store import Store
from wsb.tickers import build_universe

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

COLUMNS = ["rank", "ticker", "name", "mentions", "unique_authors",
           "weighted_score", "from_posts", "is_etf"]


def main() -> int:
    # 输出重定向到文件时 Python 默认块缓冲，攒够 8KB 才写，
    # 后台跑长任务会让 tail -f 长时间看不到任何进度。改成行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=1.0, help="回溯天数，默认 1")
    ap.add_argument("--pace", type=float, default=1.0, help="每次请求间隔秒数，默认 1.0")
    ap.add_argument("--no-scrape", action="store_true", help="跳过抓取，只统计本地缓存")
    ap.add_argument("--no-posts", action="store_true", help="只抓评论，不抓帖子")
    ap.add_argument("--top", type=int, default=200, help="输出前 N 名，默认 200")
    ap.add_argument("--refresh-universe", action="store_true", help="强制刷新上市清单")
    args = ap.parse_args()

    end = int(time.time())
    start = end - int(args.days * 86400)

    print(f"统计区间: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(start))} ~ "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(end))} UTC "
          f"（{args.days} 天）")

    print("加载上市公司清单...")
    universe = build_universe(DATA, refresh=args.refresh_universe)
    etfs = sum(1 for v in universe.values() if v.get("is_etf"))
    print(f"  词表 {len(universe)} 个代码（含 {etfs} 只 ETF）")

    store = Store(DATA / "wsb.sqlite")

    if not args.no_scrape:
        if reddit_available():
            print("提示: Reddit 公开端点当前可用，可作为实时补充通道")
        print("开始抓取（可随时 Ctrl-C 中断，下次运行自动续抓）...")
        try:
            stats = scrape(store, start, end, pace=args.pace,
                           include_posts=not args.no_posts)
            print(f"抓取完成: 新增 {stats['comments']} 条评论, {stats['posts']} 个帖子")
        except KeyboardInterrupt:
            print("\n已中断，本次抓到的数据已保存，下次运行会从断点继续。")

    print(f"本地缓存: {store.count('comments')} 条评论, {store.count('posts')} 个帖子")

    print("统计中...")
    rows = tally(store.iter_texts(start, end), universe)
    if not rows:
        print("区间内没有数据，先运行一次抓取。", file=sys.stderr)
        store.close()
        return 1

    rows = rows[:args.top]
    stamp = time.strftime("%Y%m%d_%H%M", time.gmtime(end))
    out = ROOT / f"wsb_mentions_{int(args.days)}d_{stamp}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"\n已保存 {len(rows)} 条 -> {out.name}")
    print(f"\n{'#':>3}  {'代码':<7} {'提及':>6} {'独立作者':>8} {'加权赞':>8}  名称")
    for r in rows[:20]:
        tag = " [ETF]" if r["is_etf"] else ""
        print(f"{r['rank']:>3}  {r['ticker']:<7} {r['mentions']:>6} "
              f"{r['unique_authors']:>8} {r['weighted_score']:>8}  "
              f"{r['name'][:38]}{tag}")

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
