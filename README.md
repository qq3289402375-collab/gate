# Gate 行情哨兵

按 15 分钟收盘扫描 Gate USDT 永续公开行情，输出带价格和形态依据的只读观察提醒。Python 3.11 即可运行。

## 运行

```sh
python sentinel.py --dry-run
python sentinel.py --replay-days 21
python sentinel.py --status
python -m unittest discover -s tests -v
```

- `--dry-run` 使用临时状态，仅访问 Gate 公开行情，输出扫描覆盖和入选信号。
- `--replay-days N` 回放最近 N 个完整 UTC 日，支持 1–28 天。报告包含各阈值、逐日入选数、缺失数据及 1 小时和 4 小时的方向一致率和 ATR 标准化价格变化。名单取运行当天的在市合约。样本会受当前名单和行情阶段影响，后续价格统计未计交易成本。
- `python sentinel.py` 运行一轮。`python sentinel.py --watch` 在常驻机器上于每刻钟收盘后约 3 分钟运行一轮。
- `SENTINEL_STATE` 或 `--state` 可指定本地 SQLite 文件，默认 `.sentinel/state.sqlite3`。`--status` 可查看最近扫描、通知回执和 AI 用量。独立部署应各有状态文件，同一个提醒渠道宜由一个部署运行。

## 扫描和评分

- 优先保留 5 个加密自选以及 8 个 TradFi 自选。Gate 元数据确认合约仍在交易后，按加密合约 24 小时成交额补到 50 个。元数据缺失或合约停用时会记录覆盖变化。
- 扫描最近确认收盘的 15m K 线，每次最多补扫 4 根。1H 趋势只使用信号时点已经收盘的数据。跳过有断档、异常价格重置或当前零成交的窗口。
- 同时检测刺破区间后收回、突破后首次回踩确认。评分参考 ATR、相对成交量、收盘位置和小时趋势。默认门槛 94 分，由 `SENTINEL_SCORE` 或 `--threshold` 调整。分数用来排序。单标的冷却 90 分钟，同一时段、品类、方向最多保留 2 条。每天入选量随行情变化。
- 信号按合约和 K 线时间持久去重。每轮将多个信号合并成一条消息。投递状态不明时保留记录，避免重复发送。

### 初始回放

2026-10-31 至 2026-11-20（UTC），名单包含 42 个加密合约和 8 个 TradFi 合约。50 个标的均完成下载，100,800 个应扫时段中有 100,023 个进入判定，713 个零成交，64 个遇到价格重置窗口。

| 分数门槛 | 前 14 天日均 | 后 7 天日均 | 21 天日均 |
| --- | ---: | ---: | ---: |
| 93 | 6.86 | 6.57 | 6.76 |
| 94 | 5.36 | 4.86 | 5.19 |
| 95 | 4.64 | 3.86 | 4.38 |

94 分下，后 7 天共 34 条。按信号方向计算，后续 1 小时的价格变化中位数为 -0.253 ATR，4 小时为 +1.024 ATR。回放使用扫描当天的名单，实际提醒频率与后续表现仍需用上线后的记录观察。

## 通知与 GitHub Actions

| 配置 | 用途 |
| --- | --- |
| `QQ_EMAIL`、`QQ_AUTH_CODE` | QQ 邮箱和 SMTP 授权码，发给同一邮箱 |
| `PUSHPLUS_TOKEN` | 可选的微信推送渠道 |
| `DEEPSEEK_API_KEY` | 可选的 DeepSeek API 密钥 |
| `SENTINEL_DEEPSEEK=1` | 开启 DeepSeek 对现有行情事实的摘要 |
| `SENTINEL_AI_DAILY_CAP` | 每 UTC 日最多请求次数，默认 8 次 |

GitHub Actions 的密钥通过仓库 Secrets 配置。`SENTINEL_SCORE`、`SENTINEL_DEEPSEEK` 和 `SENTINEL_AI_DAILY_CAP` 在仓库 Variables 配置。工作流手动运行时默认只读试跑。真实通知限默认分支的定时任务和显式关闭试跑的手动任务。PR 只运行本地模拟测试。

QQ 返回 SMTP 受理状态。PushPlus 返回排队流水号，可在渠道侧核对最终结果。DeepSeek 摘要只引用已有行情字段，响应异常时继续发送规则提醒。

Actions 调度可能延迟或漏跑，SQLite 状态通过 GitHub 缓存跨轮恢复。缓存失效时会从最近一根已收盘 K 线恢复扫描，去重和当日计数也会重新开始。需要长期保留状态时，可在具备 Gate 出站连接的常驻主机运行 `--watch`，把 SQLite 存在持久磁盘。

[Gate 永续行情文档](https://www.gate.com/docs/developers/apiv4/en/futures/) · [GitHub 定时与默认分支规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) · [GitHub 缓存参考](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching) · [PushPlus 回执](https://www.pushplus.plus/doc/guide/api.html) · [DeepSeek 模型与计费](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
