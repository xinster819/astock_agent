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

| 层 | 账户 | 决策方式 | 命令 | 组合风控 |
|---|---|---|---|:--:|
| 对照基线 | A 组 | 纯规则（上穿 MA20） | `astock run A` | ✗ |
| 规则实验 | exp1–exp9 | 9 种信号族，配置驱动 | `astock run all` | ✓ |
| Agent 决策 | B 组 | Agent 自主决策 | 三段式流水 | ✓ |
| Agent 决策 | C 组 | 多空辩论 | 三段式流水（注入新闻） | ✓ |
| Agent 决策 | D 组 | 新闻情绪 | 三段式流水（注入新闻） | ✓ |

13 个账户跑的是**同一份** `round_engine`，差异只有两处：指令从哪来（`decide` 回调），
以及启用哪些闸门（`RoundPolicy`）。

> **A 组为什么不加组合风控？** 它是对照基线，整个项目的价值就在于它与其他组可比。
> 给它补上风控会改变它的成交行为，等于把对照组也变成实验组——那是在破坏实验，
> 不是在修 bug。而账户互斥锁与冷却去抖对所有账户一律启用：
> 它们只拦"同一轮被触发两次"，不改变任何策略语义。

账本隔离靠 `AccountPaths`：A 组走工作区根，B/C/D 落 `group<X>/`，exp* 走 `experiments/`。

### 9 种信号族（`strategy/signals.py` 的 `signal_logic`）

`cross_up_ma20`（基准）、`cross_up_ma10`（放宽）、`cross_up_ma30`（严格）、
`ma5_cross_ma20`（真金叉——要求穿越事件本身，不是"已多头"）、`pure_momentum`、
`mean_reversion`（RSI 超卖 + 仍在中期趋势之上）、`quality_breakout`（放量确认）、
`factor_rank`（多因子横截面合成分）。

各组参数在 `config/experiments/exp*.json`，共用同一套卖出逻辑（止损/止盈/时间止损/
跌破 MA10）、仓位再平衡与突破距离过滤。

每个族是 `strategy/families.py` 里的一个纯函数，可独立测试；加第 10 个族只需
加一个函数并挂上 `@family("名字")`。未注册的 `signal_logic` 会在轮次开始时直接
报错——旧实现里拼错名字的后果是该账户**对全池所有票都不买、永久静默停止交易**，
而权益曲线照常写、闸门全绿。

配置加载时会做一致性校验：`ma_slow` 与 `signal_logic` 矛盾会直接报错。
这类"看起来改了、实际不参与计算"的字段最危险——调参的人以为在做对照实验，
两组却跑着同一套参数，结论是假的，而且没有任何迹象能让人察觉。

---

## 三段式 Agent 流水

```
astock prepare C   跨日结算 + 多源行情 + 技术指标 + 规则候选 +（C/D）真实新闻
                   → groupC/decision_input.json

[agent 回合]        → groupC/decision_output.json
                    {"input_ts": "...", "decisions": [...], "comment": "..."}

astock execute C   格式校验 + 新鲜度校验 + 组合硬闸 + 风控 → 落账本 → 归档
```

设计要点：**规则候选只是参谋，Agent 是决策者**，可采纳可否决；但 Agent 无法绕过
撮合规则的硬校验（涨跌停 / T+1 / 整手 / 资金）与 execute 的组合限制
（单票权重、持仓数上限、市场状态下的新开仓额度）。这些约束逐条有测试看着。

> ⚠️ **中段（agent 回合）不在本仓库内。** 它需要由你自己的受管 agent 工具链承担：
> 读 `decision_input.json`，写 `decision_output.json`。这是本项目唯一无法靠代码自动接上的环节。
> `execute` 在找不到决策文件、或决策未通过新鲜度校验时会**跳过下单**而不是空跑——
> 宁可不交易。

---

## 防御层

这部分是项目里最见功力的地方，每一处都有明确的坑作为设计动机。

| 模块 | 做什么 | 为什么 |
|---|---|---|
| `data.market` / `data.quote_sources` | 三源交叉验证（东财/新浪/腾讯），≥2 源且极差 ≤0.5% 取中位数 | 分歧则 **price=0 拒单**——脏价宁可不交易，绝不用错价成交 |
| `data.market.get_hist` | 个股日线多源兜底（东财 → 新浪 → 腾讯） | 单一数据源挂掉会让所有策略**静默不开仓**且无告警 |
| `data.source_health` | 源熔断：连续失败 3 次跳过 5 分钟，自动探测恢复 | 只省延迟，**不改判定**：熔断的源仍以 error 参与交叉验证计数 |
| `core.rules` | T+1 可用/持仓分离、100 股整手、涨跌停封板拒单、含费成本价 | 佣金万2.5(最低5) + 印花税千1 + 过户费万0.1 |
| `core.ledger` | state.json 原子写 + CSV 标准转义 | 调度器 10 分钟超时 SIGKILL 会写出半截 JSON；备注列是 agent 自由文本，一个逗号就让整行列错位 |
| `guards.trade` | 文件级账户互斥锁 + 60s 冷却去抖 | 根治并发"幽灵成交"：两进程读-改-写，后写覆盖先写 |
| `guards.risk` | 日内亏损限、最大回撤、连亏冷却、单票止损冷却 | 阈值经沪深300 近千交易日分布校准，不是拍脑袋 |
| `guards.regime` | live → last-known-good 缓存 → 冷启动默认，显式标 `degraded` | 杜绝"数据源一断就永久冻结开仓"，且假 risk_off 可与真的区分 |
| `guards.integrity` | 现金方向单调性、重复下单、trades 重放对账、负现金 | 把"发现执行 bug"从复盘的自觉降维成脚本的必检 |
| `guards.freshness` | 权益新鲜度 + **引擎停摆检测** | 见下 |
| `data.news_feed` | 真实新闻取数，每条带 published / source / url + stale 标注 | 根治"输入无新闻 → agent 凭记忆编新闻"的幻觉 |

### 关于「静默失效」

这套系统曾经出过一次事故：进程时区与交易所时区不一致，导致 `is_trading_now()` 判定错误，
**多数账户连续数周未进入下单分支**——而权益曲线照常写、账本完整性全绿、报告按时产出，
**全套闸门无一告警**。

这次事故留下了三样东西：

1. **`runtime.clock`** —— 交易所时钟单一事实源。两层防御：`enforce()` 把进程 TZ 钉死，
   `to_market()` 提供不依赖进程环境的显式时钟。关键点是**调用方传进来的朴素 datetime
   也必须归一**，只改函数默认值是修不好的。
2. **`guards.freshness.stalled_engine`** —— 直接检测"进程在跑但从未进入下单分支"。
   判据用 `round`/`last_trading_round_date` 而非"零成交"，因为高门槛策略长期无信号是正常的。
3. **`execute.decision_freshness`** —— 决策文件必须晚于本轮决策包，否则拒绝执行，
   避免"拿旧决策按新价格下单"。

结论：**静默失效比报错危险得多。** 所有闸门都遵循"宁可吵，也不沉默"。

---

## 测试

```bash
make check      # 静态检查 + 全量测试
make cov        # 带覆盖率
```

410 个用例，全部离线、无需联网，跑完约 2 秒。

「静默失效」这条原则同样适用于测试本身——**一个从不失败也从不执行的测试，
比没有测试更危险，因为它让覆盖率报告显得很好看。** 这个仓库真出过三次：

| 症状 | 后果 |
|---|---|
| 两个测试文件是 pytest 风格，而仓库没装 pytest | 靠 `python test_x.py` 跑时静默什么都不执行，从写下那天起没跑过一次 |
| 一个用例断言的是**生产账本**，数据不在就 `skipTest` | 数据一被清洗就永久跳过 |
| 护栏集成测试 stub 掉了 `_buy_exp` —— 而那正是要被删掉的重复实现 | 名义上在验护栏接线，实际什么也没拦 |

对应的三条基础设施：

- **`pyproject.toml`** 固定 pytest 配置（`--strict-markers` / `--strict-config`），
  CI 里另有一条 job 专门检查"有没有测试被跳过"，有就直接失败。
- **`tests/conftest.py`** 给每个用例一个独立临时工作区，并复位跨用例存活的
  全局单例（源熔断器、指标缓存）。这些用例过去单跑全绿、合跑就红。
- **`tests/unit/test_layering.py`** 把架构约束变成可执行的检查：解析每个模块的
  import，回指上层就失败；`core`/`runtime` 引入网络库就失败；模块顶层做 IO 就失败。
  最后一条正是当年 `broker.py` 的原罪。

覆盖率上，只对 `core` / `runtime`（钱和账本）设 90% 硬门槛。取数与报表层大量依赖
外网，设门槛只会逼出假测试。

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

推荐用 `make install`，它会一并装上开发依赖（pytest / ruff / mypy）。

### 自检

```bash
make check                    # 静态检查 + 全量测试
make cov                      # 带覆盖率
astock doctor                 # 时钟 / 工作区 / 13 个账户账本是否就位
astock check                  # 各账户账本完整性对账
```

测试全部离线，不需要联网。

### 运行

所有操作走同一个命令行入口：

```bash
astock run A                  # A 组（对照基线）
astock run exp1               # 单个实验组
astock run all                # 全部 9 个实验组

astock prepare C              # Agent 组第一段 → groupC/decision_input.json
#   → 你的 agent 读 decision_input.json，写 decision_output.json
astock execute C              # 第三段：校验 → 硬闸 → 落账本 → 归档

astock report                 # 13 个账户横向对比表
astock report exp4            # 单账户详情
astock dashboard              # 生成 reports/dashboard.html
astock weekly                 # 周度数据底座
```

未安装包时用 `.venv/bin/python -m astock.cli.main <子命令>`，行为完全一致。

常用开关：`--force` 跳过交易时段判断强制成交（**只放行时段判断**，涨跌停 /
T+1 / 整手 / 资金 / 组合限制 / 风控一律照旧），`--no-jitter` 关闭随机延时。
强制轮次会在成交备注里打 `[强制/非交易时段]` 标记，让账本自证。

非交易时段默认只刷新估值、不下单。

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

## 架构

依赖**只允许自上而下**，禁止回指。这条约束不是文档里的君子协定——
`tests/unit/test_layering.py` 会解析每个模块的 import 逐条检查，
新增一条回指依赖会让测试变红。

```
astock/
  cli/         统一命令行入口（astock run / prepare / execute / report …）
  ops/         运维脚本：抖动核查、幽灵成交清理
    ↓
  pipeline/    轮次编排
    round_engine.py   ★ 唯一的一份"推进一轮"实现，13 个账户共用
    run_rule.py       规则组（A + exp1~exp9）：挑参数，其余交给引擎
    prepare.py        Agent 三段式第一段：生成决策包
    execute.py        第三段：校验决策 → 组合硬闸 → 落地 → 归档
    exp_scheduler.py  批量推进实验组，账户间故障隔离
    ↓
  guards/      闸门：宁可吵，也不沉默
    risk.py       日内亏损 / 回撤 / 连亏冷却
    trade.py      账户互斥锁 + 冷却去抖（防幽灵成交）
    integrity.py  trades 重放对账 / 现金单调性 / 负现金
    freshness.py  权益新鲜度 + 引擎停摆检测
    regime.py     市场状态（live → 缓存 → 冷启动默认，降级显式标记）
  reporting/   只读产出层，绝不写账本
    roster.py         13 个账户的名册，名称以 config 为权威
    metrics.py        周窗口 / 权益取点 / 胜负统计（纯函数）
    report.py  weekly.py  dashboard.py
  strategy/    信号生成（纯计算，不做 IO 决策）
    families.py       9 种买入信号族的注册表，一族一个纯函数
    signals.py        共用的卖出逻辑、仓位再平衡、下单量计算
    ↓
  data/        取数：多源交叉验证、源熔断、日线多源兜底、真实新闻
    ↓
  core/        ★ 钱的唯一真相
    fees.py     费率
    rules.py    撮合规则（纯函数、零 IO，可内存测试）
    ledger.py   账本落盘（原子写 + 标准 CSV 转义）
    account.py  账户门面
    ↓
  runtime/     基础设施，无业务语义
    clock.py    交易所时钟（单一事实源）
    paths.py    账本路径（运行期解析，非 import 期）
    files.py    原子 JSON 写 / CSV 追加
    jitter.py   调度抖动 + 截断检测日志
```

`core` 与 `runtime` 不 import 上层，也不碰网络——撮合规则一旦能发请求，
就再也没法在内存里确定性地测它。这两条同样由分层测试强制。

### 目录约定

| 目录 | 内容 | 入库 | 丢了会怎样 |
|---|---|:--:|---|
| `astock/` `tests/` | 代码与测试 | ✅ | — |
| `config/` | 实验组参数、采样池 | ✅ | 改了要评审 |
| `reports/` | 看板、周度数据底座 | ❌ | 重跑一次就有 |
| `docs/` | 交接与复盘文档（含账本数字） | ❌ | — |
| `experiments/` `groupB/C/D/`<br>根目录 `state/trades/equity` | **账本** | ❌ | **不可恢复** |

配置与账本物理分开，是为了让 `.gitignore` 不必再靠文件名模式去猜
哪个 `experiments/exp1_*.json` 是参数、哪个是账本。

工作区位置可用 `$ASTOCK_HOME` 重定向（默认=仓库根），配置根用 `$ASTOCK_CONFIG`。
测试正是靠这个把每个用例隔离到独立临时目录。

---

## 数据源

`akshare` + 东方财富 / 新浪 / 腾讯的公开行情接口。全部只读 HTTP，不起任何服务。

首次运行需能访问公网行情与新闻接口。任一源不可用时系统会降级并**显式标记**，
而不是静默使用可疑数据。
