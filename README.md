# astock_agent

A 股多策略**模拟**交易系统。13 个相互隔离的虚拟账户并行运行，用不同的策略与决策方式互为对照。

> **边界（贯穿整个项目的硬约束）**
> - 本地模拟盘：**不接实盘、不下真实委托**。所有"成交"只写进本地 CSV 账本。
> - **不伪造、不回填净值**：数据缺失就如实标缺失，绝不当作当期业绩。
> - 脚本禁止直连任何 LLM 网关；禁止启动网络监听进程。
>
> 本仓库**不含账本数据**（成交流水 / 权益曲线 / 持仓状态 / 决策记录）。
> 首次运行会自动以初始现金初始化，从零开始。

---

## 设计目标

核心价值是**对照实验**，不是收益本身——回答"规则决策 vs Agent 决策""不同信号族之间"孰优。
所以系统的重心不在策略有多聪明，而在于：

1. **账本可信**：任何一笔成交都要能被重放校验，账实不符必须能被自动发现。
2. **失败要响**：宁可大声报错、宁可不交易，也不允许"看起来正常"地空转。

第 2 条是踩过坑换来的。详见下面「静默失效」一节。

---

## 账户分层

| 层 | 账户 | 决策方式 | 驱动 |
|---|---|---|---|
| 对照基线 | A 组 | 纯规则（上穿 MA20） | `run.py` |
| 规则实验 | exp1–exp9 | 9 种信号族，配置驱动 | `run_all_exp.py` |
| Agent 决策 | B 组 | Agent 自主决策 | 三段式流水 |
| Agent 决策 | C 组 | 多空辩论 | 三段式流水（注入新闻） |
| Agent 决策 | D 组 | 新闻情绪 | 三段式流水（注入新闻） |

隔离靠环境变量 `ASTOCK_GROUP`：A 组走根目录账本，B/C/D 落 `group<X>/`，exp* 走 `experiments/`。

### 9 种信号族（`strategy.py` 的 `signal_logic`）

`cross_up_ma20`（基准）、`cross_up_ma10`（放宽）、`cross_up_ma30`（严格）、
`ma5_cross_ma20`（真金叉——要求穿越事件本身，不是"已多头"）、`pure_momentum`、
`mean_reversion`（RSI 超卖 + 仍在中期趋势之上）、`quality_breakout`（放量确认）、
`factor_rank`（多因子横截面合成分）。

各组参数在 `experiments/exp*.json`，共用同一套卖出逻辑（止损/止盈/时间止损/跌破 MA10）、
仓位再平衡与突破距离过滤。

---

## 三段式 Agent 流水

```
prepare.py   跨日结算 + 多源行情 + 技术指标 + 规则候选 +（C/D）真实新闻
             → group<X>/decision_input.json

[agent 回合]  → group<X>/decision_output.json
              {"input_ts": "...", "decisions": [...], "comment": "..."}

execute.py   格式校验 + 新鲜度校验 + 组合硬闸 + 风控 → broker 落地 → 归档
```

设计要点：**规则候选只是参谋，Agent 是决策者**，可采纳可否决；但 Agent 无法绕过
broker 的硬校验和 execute 的组合限制。

> ⚠️ **中段（agent 回合）不在本仓库内。** 它需要由你自己的受管 agent 工具链承担：
> 读 `decision_input.json`，写 `decision_output.json`。这是本项目唯一无法靠代码自动接上的环节。
> `execute.py` 在找不到决策文件时会**跳过下单**而不是空跑——宁可不交易。

---

## 防御层

这部分是项目里最见功力的地方，每一处都有明确的坑作为设计动机。

| 模块 | 做什么 | 为什么 |
|---|---|---|
| `market` / `quote_sources` | 三源交叉验证（东财/新浪/腾讯），≥2 源且极差 ≤0.5% 取中位数 | 分歧则 **price=0 拒单**——脏价宁可不交易，绝不用错价成交 |
| `market.get_hist` | 个股日线多源兜底（东财 → 新浪 → 腾讯） | 单一数据源挂掉会让所有策略**静默不开仓**且无告警 |
| `source_health` | 源熔断：连续失败 3 次跳过 5 分钟，自动探测恢复 | 只省延迟，**不改判定**：熔断的源仍以 error 参与交叉验证计数 |
| `broker` | T+1 可用/持仓分离、100 股整手、涨跌停封板拒单、含费成本价 | 佣金万2.5(最低5) + 印花税千1 + 过户费万0.1 |
| `trade_guard` | 文件级账户互斥锁 + 60s 冷却去抖 | 根治并发"幽灵成交"：两进程读-改-写，后写覆盖先写 |
| `risk_guard` | 日内亏损限、最大回撤、连亏冷却、单票止损冷却 | 阈值经沪深300 近千交易日分布校准，不是拍脑袋 |
| `market_regime` | live → last-known-good 缓存 → 冷启动默认，显式标 `degraded` | 杜绝"数据源一断就永久冻结开仓"，且假 risk_off 可与真的区分 |
| `integrity_gate` | 现金方向单调性、重复下单、trades 重放对账、负现金 | 把"发现执行 bug"从复盘的自觉降维成脚本的必检 |
| `freshness_gate` | 权益新鲜度 + **引擎停摆检测** | 见下 |
| `news_feed` | 真实新闻取数，每条带 published / source / url + stale 标注 | 根治"输入无新闻 → agent 凭记忆编新闻"的幻觉 |

### 关于「静默失效」

这套系统曾经出过一次事故：进程时区与交易所时区不一致，导致 `is_trading_now()` 判定错误，
**多数账户连续数周未进入下单分支**——而权益曲线照常写、账本完整性全绿、报告按时产出，
**全套闸门无一告警**。

这次事故留下了三样东西：

1. **`market_time.py`** —— 交易所时钟单一事实源。两层防御：`enforce()` 把进程 TZ 钉死，
   `to_market()` 提供不依赖进程环境的显式时钟。关键点是**调用方传进来的朴素 datetime
   也必须归一**，只改函数默认值是修不好的。
2. **`freshness_gate.stalled_engine`** —— 直接检测"进程在跑但从未进入下单分支"。
   判据用 `round`/`last_trading_round_date` 而非"零成交"，因为高门槛策略长期无信号是正常的。
3. **`execute.decision_freshness`** —— 决策文件必须晚于本轮决策包，否则拒绝执行，
   避免"拿旧决策按新价格下单"。

结论：**静默失效比报错危险得多。** 所有闸门都遵循"宁可吵，也不沉默"。

---

## 快速开始

### 环境搭建

需要 Python 3.9+（建议 3.11，`akshare` 依赖链 wheel 覆盖最好）。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

<details>
<summary>如果你的网络有 TLS 中间人（企业安全网关）</summary>

部分企业网络会经安全网关做 TLS 中间人，服务器证书由企业自签根 CA 签发。
该 CA 通常已在系统信任库中（所以 `curl` 能直连），但 Python 的 `ssl`/`requests`
默认走 certifi，不含它 → `CERTIFICATE_VERIFY_FAILED`。

正解是**补全信任链，不是关闭校验**。macOS 上：

```bash
mkdir -p .venv/ssl
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain >  .venv/ssl/ca-bundle.pem
security find-certificate -a -p /Library/Keychains/System.keychain                        >> .venv/ssl/ca-bundle.pem
export SSL_CERT_FILE="$PWD/.venv/ssl/ca-bundle.pem" REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
```

想让它对 cron/launchd 也自动生效，可在 venv 的 `site-packages/sitecustomize.py` 里
`os.environ.setdefault` 这两个变量。

> ⚠️ 任何情况下都不要用 `verify=False` 或 `pip --trusted-host` 绕过证书校验。
> 生成的 CA bundle **不要提交到版本库**（`.gitignore` 已排除）。

</details>

### 自检

```bash
.venv/bin/python -m unittest discover     # 全量测试
.venv/bin/python integrity_gate.py        # 各账户账本完整性
.venv/bin/python market_time.py           # 交易所时钟体检
```

### 运行

```bash
.venv/bin/python run.py --no-jitter                      # A 组
.venv/bin/python run_exp.py exp1 --no-jitter             # 单个实验组
.venv/bin/python run_all_exp.py --no-jitter              # 全部实验组

ASTOCK_GROUP=C .venv/bin/python prepare.py --no-jitter   # Agent 组第一段
#   → 你的 agent 读 groupC/decision_input.json，写 groupC/decision_output.json
ASTOCK_GROUP=C .venv/bin/python execute.py               # 第三段

.venv/bin/python dashboard.py                            # 生成 dashboard.html
.venv/bin/python weekly_collect.py                       # 周度数据底座
```

非交易时段默认只刷新估值、不下单。`--force` 可强制走下单分支（用于测试），
`execute.py` 的强制轮次会在成交备注里打 `[强制/非交易时段]` 标记，让账本自证。

### 调度（macOS）

`scheduler_tick.sh` 是心跳脚本：**每小时被唤醒一次，由脚本自己按交易所时区判断该不该跑**。

不把北京时刻硬换算成本地时刻写进 cron，是因为跨时区部署 + 夏令时会让换算每年错开两次，
而且错开时不报错。改成"多触发几次 + 脚本自行把关"后，时区漂移免疫。

```bash
./install_scheduler.sh
```

> ⚠️ **项目不能放在 `~/Desktop` / `~/Documents` / `~/Downloads` 下。**
> 这些目录受 macOS TCC 保护，launchd 拉起的进程读不到里面的文件，
> 任务会每轮静默失败而 `launchctl list` 看起来一切正常。
> `install_scheduler.sh` 会检测并拒绝安装。
>
> 装完**立刻实测一次**，别只看 `load` 成功：
> ```bash
> launchctl kickstart -k gui/$(id -u)/com.astock.agent
> ```
> 然后确认 `logs/launchd.err.log` 为空、`logs/tick_<日期>.log` 已生成。

---

## 目录结构

```
核心引擎
  market.py / quote_sources.py   多源交叉验证行情 + 日线多源兜底
  market_time.py                 交易所时钟（单一事实源）
  source_health.py               数据源熔断
  broker.py                      A 股规则撮合与账本
  strategy.py                    9 种信号族 + 仓位再平衡（含权重死区）
  run.py / run_exp.py / run_all_exp.py / exp_scheduler.py
  prepare.py / execute.py / news_feed.py     Agent 三段式流水

风控与闸门
  risk_guard.py  trade_guard.py  integrity_gate.py
  freshness_gate.py  market_regime.py

观测与复盘
  dashboard.py  weekly_collect.py  check_jitter.py
  report.py  report_exp.py  clean_ghost_trades.py

配置与调度
  experiments/exp*.json          各实验组参数
  sample_pool.json               价差采样池
  scheduler_tick.sh  install_scheduler.sh  com.astock.agent.plist

测试
  test_*.py                      unittest，无需联网
```

---

## 数据源

`akshare` + 东方财富 / 新浪 / 腾讯的公开行情接口。全部只读 HTTP，不起任何服务。

首次运行需能访问公网行情与新闻接口。任一源不可用时系统会降级并**显式标记**，
而不是静默使用可疑数据。
