# Retail Investor Crowding Detection

[English](README.md) | 中文

识别 r/wallstreetbets（WSB）中被集中讨论，并具有做空或期权异动特征的股票。

项目直接从 WSB 帖子与评论中统计股票提及量，再补充价格、做空比例、借券成本、隐含波动率和波动率偏斜等指标。无需 Reddit API Key。

## 快速开始

需要 Python 3、`curl` 和 `requests`：

```bash
pip install requests
python3 wsb_mentions.py --days 7
python3 short_metrics.py -i wsb_mentions_7d_YYYYMMDD_HHMM.csv
```

将第二行中的文件名替换为第一步实际生成的文件。抓取一周数据可能需要数小时，可按 `Ctrl-C` 中断，下次运行会从断点继续。

## 输出

- `wsb_mentions_*.csv`：股票提及排行
- `wsb_mentions_*_short.csv`：加入做空、借券和波动率指标后的结果

主要提及指标：

- `mentions`：提及该股票的帖子/评论数
- `unique_authors`：提及该股票的不同用户数
- `weighted_score`：相关内容的 upvote 总数

主要市场指标：

- `short_float_pct`：空头占自由流通股比例
- `cost_to_borrow_pct`：年化借券成本
- `iv_30d_tenor`：约 30 天期限的隐含波动率
- `iv_skew_30d`：期权波动率偏斜；负值表示 call 侧相对升温

## 数据与说明

数据来自 Arctic Shift、Finviz、IBKR 和 AlphaQuery 等公开来源。本地抓取缓存保存在 `data/wsb.sqlite`。

本项目仅用于研究和数据分析，不构成投资建议。
