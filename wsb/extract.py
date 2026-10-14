"""从文本里提取股票代码并统计。

口径（去重计数）：
  mentions          一条评论/帖子里同一代码重复出现只算 1 次
  unique_authors    提到该代码的不同用户数 —— 最抗刷屏的指标
  weighted_score    各条提及所在内容的 upvote 之和，衡量「被看见的程度」

$TSLA 这种带前缀的写法可信度高，哪怕是歧义词也计入；裸写的 TSLA 则必须
通过白名单且不在歧义词表里才算。
"""

import re
from collections import defaultdict

from .tickers import AMBIGUOUS

# $AAPL / $BRK.B —— 带 $ 前缀，强信号
CASHTAG = re.compile(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")
# 裸写的大写代码，必须查白名单
BARE = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")

# 代码块、链接、引用里的大写词不是讨论内容，先剥掉
NOISE = re.compile(
    r"(```.*?```)"          # 围栏代码块
    r"|(`[^`]*`)"           # 行内代码
    r"|(https?://\S+)"      # 链接
    r"|(/?[ru]/[A-Za-z0-9_-]+)"  # /r/xxx 和 /u/xxx
    r"|(&[a-z]+;)",         # HTML 实体
    re.S,
)


def extract(text: str, universe: dict) -> set:
    """返回文本中出现的代码集合（已按条去重）。"""
    if not text:
        return set()
    clean = NOISE.sub(" ", text)

    found = set()
    # $ 前缀：只要在白名单里就认，不受歧义词表限制
    for m in CASHTAG.finditer(clean):
        sym = m.group(1).upper()
        if sym in universe:
            found.add(sym)

    # 裸写：必须在白名单且不是歧义词
    for m in BARE.finditer(clean):
        sym = m.group(1)
        if sym in universe and sym not in AMBIGUOUS:
            found.add(sym)

    return found


def tally(rows, universe: dict) -> list:
    """rows 为 (text, author, score, kind) 流，返回按提及量排序的统计。"""
    mentions = defaultdict(int)
    authors = defaultdict(set)
    weighted = defaultdict(int)
    from_posts = defaultdict(int)

    for text, author, score, kind in rows:
        for sym in extract(text, universe):
            mentions[sym] += 1
            if author and author != "[deleted]":
                authors[sym].add(author)
            weighted[sym] += max(score, 0)
            if kind == "post":
                from_posts[sym] += 1

    out = []
    for sym, n in mentions.items():
        info = universe.get(sym, {})
        out.append({
            "ticker": sym,
            "name": info.get("name", ""),
            "is_etf": info.get("is_etf", False),
            "mentions": n,
            "unique_authors": len(authors[sym]),
            "weighted_score": weighted[sym],
            "from_posts": from_posts[sym],
        })
    # 同票时用独立作者数破平，更能反映真实热度
    out.sort(key=lambda r: (r["mentions"], r["unique_authors"]), reverse=True)
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out
