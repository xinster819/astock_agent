"""
策略层：自包含多因子/均线策略（无人值守，无需调用大模型）。
设计目标：逻辑透明、可改。你可以把 generate_signals 换成任何你想要的逻辑，
甚至接 LLM 决策——只要返回同样格式的 signals 即可。

默认股票池：可在 watchlist.json 配置；缺省给一篮子流动性好的大盘股。
信号规则：
  买入：收盘价上穿 MA20 且 MA5>MA20（多头）且当日未涨停；按20日动量打分排序。
  卖出：收盘价跌破 MA10，或相对成本亏损 <= -8%（止损），或盈利 >= +20%（止盈）。
风控：单票上限 20% 总资产；最多持仓 5 只；每轮最多新开 2 笔。
"""
import datetime as dt
import json
import os

from astock.runtime import paths

MA_FAST, MA_MID, MA_SLOW = 5, 10, 20
MAX_POSITIONS = 5
MAX_WEIGHT = 0.20          # 单票上限占总资产
MAX_NEW_PER_ROUND = 2
STOP_LOSS = -0.08
TAKE_PROFIT = 0.20

DEFAULT_POOL = [
    # 白酒消费 (6只)
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "000568",  # 泸州老窖
    "002304",  # 洋河股份
    "600809",  # 山西汾酒
    "600887",  # 伊利股份
    # 金融 (6只)
    "600036",  # 招商银行
    "601318",  # 中国平安
    "000001",  # 平安银行
    "601398",  # 工商银行
    "601288",  # 农业银行
    "601601",  # 中国太保
    # 新能源/汽车 (6只)
    "300750",  # 宁德时代
    "002594",  # 比亚迪
    "002812",  # 恩捷股份
    "300014",  # 亿纬锂能
    "002460",  # 赣锋锂业
    "601127",  # 赛力斯
    # 医药 (5只)
    "600276",  # 恒瑞医药
    "000538",  # 云南白药
    "603259",  # 药明康德
    "300122",  # 智飞生物
    "600763",  # 通策医疗
    # 科技/芯片 (6只)
    "688981",  # 中芯国际
    "002371",  # 北方华创
    "603501",  # 韦尔股份
    "000938",  # 中芯国际(代码可能有误，保留)
    "002049",  # 紫光国微
    "300782",  # 卓胜微
    # 电力/能源 (5只)
    "600900",  # 长江电力
    "601899",  # 紫金矿业
    "601088",  # 中国神华
    "600028",  # 中国石化
    "600011",  # 华能国际
    # 家电/制造 (5只)
    "000333",  # 美的集团
    "000651",  # 格力电器
    "600690",  # 海尔智家
    "603486",  # 科沃斯
    "002050",  # 三花智控
    # 互联网/通信 (4只)
    "601728",  # 中国电信
    "600050",  # 中国联通
    "601138",  # 工业富联
    "002236",  # 大华股份
    # 化工/材料 (4只)
    "600309",  # 万华化学
    "002493",  # 荣盛石化
    "601233",  # 桐昆股份
    "002648",  # 卫星化学
    # 军工/航空 (3只)
    "600893",  # 航发动力
    "000768",  # 中航西飞
    "002179",  # 中航光电
]


def load_pool():
    p = str(paths.watchlist())
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("pool", DEFAULT_POOL)
    return DEFAULT_POOL


def _ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _golden_cross(closes, fast=MA_FAST, slow=MA_SLOW):
    """真金叉：上一周期 MA_fast<=MA_slow 且当前 MA_fast>MA_slow（穿越事件本身）。
    仅当前 MA_fast>MA_slow（已多头）不算金叉——那是追高。数据不足返回 False。"""
    if len(closes) < slow + 1:
        return False
    pf, ps = _ma(closes[:-1], fast), _ma(closes[:-1], slow)
    cf, cs = _ma(closes, fast), _ma(closes, slow)
    if None in (pf, ps, cf, cs):
        return False
    return pf <= ps and cf > cs


# ---- 轮内指标缓存 ----
# 一轮里 _indicators(code) 会被调用 2~3 次（卖出判断 / 仓位再平衡 / 买入候选），
# 每次都是一次日线网络请求。实测单只约 4s，全池 50 只重复一遍就是 200s，
# 13 个账户叠加后一个 tick 要跑两小时，直接超过调度节奏。
# 缓存同时修掉一个隐蔽的不一致：同一轮内先后两次取数可能拿到不同的当日 bar，
# 导致"卖出用一个价、买入用另一个价"。缓存后一轮=一个一致快照。
# 由各入口（run_once / _run_experiment_locked / build）在轮首显式清空。
_IND_CACHE: dict = {}


def clear_indicator_cache():
    """清空轮内指标缓存。每轮开始时调用，保证跨轮取到最新日线。"""
    _IND_CACHE.clear()


def _indicators(code):
    """拉最近60日线，算 MA5/10/20 与 20日动量。返回 dict 或 None。（轮内缓存）"""
    if code in _IND_CACHE:
        return _IND_CACHE[code]
    result = _compute_indicators(code)
    _IND_CACHE[code] = result
    return result


def _compute_indicators(code):
    from astock.data import market
    end = dt.datetime.now().strftime("%Y%m%d")
    start = (dt.datetime.now() - dt.timedelta(days=120)).strftime("%Y%m%d")
    try:
        df = market.get_hist(code, start, end, adjust="qfq")
        if df is None or len(df) < MA_SLOW + 1:
            return None
        closes = df["收盘"].astype(float).tolist()
        ma5, ma10, ma20 = _ma(closes, MA_FAST), _ma(closes, MA_MID), _ma(closes, MA_SLOW)
        prev_ma20 = _ma(closes[:-1], MA_SLOW)
        last = closes[-1]
        prev = closes[-2]
        mom = (closes[-1] / closes[-MA_SLOW] - 1) if len(closes) >= MA_SLOW else 0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = deltas[-14:]
        gains = sum(x for x in recent if x > 0) / 14
        losses = -sum(x for x in recent if x < 0) / 14
        rsi14 = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
        volume_ratio = None
        if "成交量" in df.columns and len(df) >= 21:
            volumes = df["成交量"].astype(float).tolist()
            average_volume = sum(volumes[-21:-1]) / 20
            if average_volume > 0:
                volume_ratio = volumes[-1] / average_volume
        return {
            "code": code, "close": last, "prev_close": prev,
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "prev_ma20": prev_ma20,
            "momentum": mom, "rsi14": rsi14, "volume_ratio": volume_ratio,
            "cross_up_ma20": prev <= (prev_ma20 or last) and last > (ma20 or last),
            "below_ma10": last < (ma10 or last),
            "golden_cross": _golden_cross(closes, MA_FAST, MA_SLOW),
        }
    except Exception:
        return None


def generate_signals(st, quotes, exp_config=None):
    """
    返回信号列表：[{action:'buy'/'sell', code, qty, reason}]
    quotes: {code: quote} 实时快照（用于现价/涨跌停判断，下单时再次校验）。
    exp_config: 实验组配置，为None时使用默认参数
    """
    # 从实验组配置读取参数，或使用默认值
    cfg = exp_config or {}
    stop_loss = cfg.get("stop_loss", STOP_LOSS)
    take_profit = cfg.get("take_profit", TAKE_PROFIT)
    max_positions = cfg.get("max_positions", MAX_POSITIONS)
    max_weight = cfg.get("max_weight", MAX_WEIGHT)
    max_new_per_round = cfg.get("max_new_per_round", MAX_NEW_PER_ROUND)
    momentum_threshold = cfg.get("momentum_threshold", 0.0)
    signal_logic = cfg.get("signal_logic", "cross_up_ma20")
    # 慢线周期由 signal_logic 的名字决定（cross_up_ma10/20/30），
    # 配置里的 ma_slow 不参与计算；两者矛盾时 experiments.validate_config 会报错。
    market_regime = cfg.get("market_regime", "normal")
    max_breakout_distance = cfg.get("max_breakout_distance", 0.08)
    # 高波动环境降低开仓数量；risk_off 只允许卖出，不产生买入信号。
    effective_max_new = 0 if market_regime == "risk_off" else max_new_per_round
    if market_regime == "high_volatility":
        effective_max_new = min(effective_max_new, 1)

    signals = []
    positions = st["positions"]

    # ---- 卖出判断（先腾出仓位）----
    for code, p in list(positions.items()):
        if p.get("available", 0) <= 0:
            continue  # T+1 冻结，不可卖
        ind = _indicators(code)
        q = quotes.get(code) or {}
        px = q.get("price") or (ind["close"] if ind else p["cost"])
        pnl = (px / p["cost"] - 1) if p["cost"] else 0
        reason = None
        if pnl <= stop_loss:
            reason = f"止损 {pnl*100:.1f}%"
        elif pnl >= take_profit:
            reason = f"止盈 {pnl*100:.1f}%"
        elif cfg.get("time_stop_days") and p.get("opened_at"):
            try:
                opened_at = dt.datetime.fromisoformat(str(p["opened_at"]))
                held_days = (dt.datetime.now().date() - opened_at.date()).days
                if held_days >= int(cfg["time_stop_days"]) and pnl <= cfg.get("time_stop_min_pnl", 0.0):
                    reason = f"时间止损 {held_days}天"
            except (TypeError, ValueError):
                pass
        elif ind and ind["below_ma10"]:
            reason = "跌破MA10"
        if reason:
            signals.append({"action": "sell", "code": code,
                            "qty": p["available"], "reason": reason})

    # ---- 存量仓位再平衡：配置收紧后，下一交易轮自动把旧仓位压回风险预算。----
    from astock.core.rules import market_value
    _, portfolio_total = market_value(st, quotes)
    active_positions = []
    for code, p in positions.items():
        q = quotes.get(code) or {}
        px = q.get("price") or p.get("cost", 0)
        if px > 0 and p.get("qty", 0) > 0:
            active_positions.append((code, p, px, _indicators(code)))

    planned_sells = {s["code"] for s in signals if s["action"] == "sell"}
    # 权重死区：仅当超额显著（超过 allowed_qty 的一定比例）才再平衡，
    # 一次性卖到合规位。否则价格微涨会让 allowed_qty 缩一手，触发"越涨越割
    # 一手"的负和碎单（每手约 6 元手续费空耗）。默认 5%。
    rebalance_deadband = cfg.get("rebalance_deadband", 0.05)
    for code, p, px, _ in active_positions:
        allowed_qty = int((portfolio_total * max_weight) / px // 100 * 100)
        excess = max(0, p["qty"] - allowed_qty)
        # 死区门槛：超额须同时 >0 且超过 allowed 的死区比例（allowed 为 0 时退化为
        # 只要有超额即处理，避免整仓不合规却被死区吞掉）。
        threshold = allowed_qty * rebalance_deadband
        if excess <= threshold:
            continue
        sellable = min(excess, p.get("available", 0))
        if code not in planned_sells and sellable > 0:
            signals.append({"action": "sell", "code": code, "qty": sellable,
                            "reason": "仓位再平衡: 单票权重上限"})
            planned_sells.add(code)

    remaining = [item for item in active_positions if item[0] not in planned_sells]
    if len(remaining) > max_positions:
        # 优先退出动量最弱的超额仓位；同动量时按代码稳定排序，保证可复现。
        for code, p, _, _ind in sorted(
            remaining, key=lambda item: ((item[3] or {}).get("momentum", float("-inf")), item[0])
        )[:len(remaining) - max_positions]:
            qty = p.get("available", 0)
            if qty > 0:
                signals.append({"action": "sell", "code": code, "qty": qty,
                                "reason": "仓位再平衡: 持仓数量上限"})
                planned_sells.add(code)

    # ---- 买入判断 ----
    held = set(positions.keys())
    slots = max_positions - len([c for c in held
                                 if c not in [s["code"] for s in signals if s["action"] == "sell"]])
    if slots > 0:
        candidates = []
        for code in load_pool():
            if code in held:
                continue
            ind = _indicators(code)
            if not ind:
                continue

            # 根据信号逻辑判断是否买入
            bullish = (ind["ma5"] and ind["ma20"] and ind["ma5"] > ind["ma20"])
            should_buy = False

            if signal_logic == "cross_up_ma20":
                # 基准策略：上穿MA20 + 多头排列 + 动量>0
                should_buy = ind["cross_up_ma20"] and bullish and ind["momentum"] > momentum_threshold
            elif signal_logic == "cross_up_ma10":
                # 放宽策略：上穿MA10 + 多头排列 + 动量>-3%
                ma10 = ind["ma10"]
                prev_close = ind["prev_close"]
                cross_up_ma10 = prev_close <= ma10 and ind["close"] > ma10 if ma10 else False
                should_buy = cross_up_ma10 and bullish and ind["momentum"] > momentum_threshold
            elif signal_logic == "cross_up_ma30":
                # 严格策略：上穿MA30 + 多头排列 + 动量>5%
                # 需要计算MA30
                from astock.data import market
                end = dt.datetime.now().strftime("%Y%m%d")
                start = (dt.datetime.now() - dt.timedelta(days=120)).strftime("%Y%m%d")
                try:
                    df = market.get_hist(code, start, end, adjust="qfq")
                    if df is not None and len(df) >= 31:
                        closes = df["收盘"].astype(float).tolist()
                        ma30 = _ma(closes, 30)
                        prev_ma30 = _ma(closes[:-1], 30)
                        cross_up_ma30 = closes[-2] <= prev_ma30 and closes[-1] > ma30 if prev_ma30 else False
                        should_buy = cross_up_ma30 and bullish and ind["momentum"] > momentum_threshold
                except Exception as exc:
                    # 绝不静默：取不到 MA30 时 exp3 会整轮无买入信号，
                    # 与"确实没信号"表现完全一致——不说出来就无从分辨。
                    print(f"⚠ {code} MA30 计算失败，本轮该票无 cross_up_ma30 信号：{exc!r}"[:120])
            elif signal_logic == "ma5_cross_ma20":
                # 金叉策略：MA5「上穿」MA20 的真穿越事件 + 动量达标。
                # 修复「假金叉」——旧实现只判当前 ma5>ma20（已多头即买），
                # 会追高接盘；现在要求上一周期 ma5<=ma20、当前 ma5>ma20 的穿越本身。
                should_buy = ind.get("golden_cross", False) and ind["momentum"] > momentum_threshold
            elif signal_logic == "pure_momentum":
                # 动量组也需要趋势与成交量确认，避免弱市追逐反弹。
                should_buy = (
                    ind["momentum"] > momentum_threshold
                    and bullish
                    and (ind.get("volume_ratio") is None
                         or ind["volume_ratio"] >= cfg.get("min_volume_ratio", 1.0))
                )
            elif signal_logic == "mean_reversion":
                # 震荡市均值回归：短期超卖、仍在中期趋势之上，等待回归而非追涨。
                rsi14 = ind.get("rsi14")
                should_buy = (
                    rsi14 is not None
                    and rsi14 <= cfg.get("rsi_buy_max", 35)
                    and ind["close"] >= ind["ma20"] * cfg.get("ma20_floor", 0.97)
                    and ind["momentum"] >= momentum_threshold
                )
            elif signal_logic == "quality_breakout":
                # 质量突破：趋势、MA20 上穿、相对成交量与温和动量共同确认。
                # breakout_relaxed=True 时把"当日上穿 MA20"（罕见同 bar 事件，
                # 曾致全周 0 单）放宽为"站上/上穿 MA20"，但放量+动量+多头仍是硬门槛。
                volume_ratio = ind.get("volume_ratio")
                breakout_relaxed = cfg.get("breakout_relaxed", False)
                above_ma20 = ind.get("ma20") and ind["close"] >= ind["ma20"]
                breakout_ok = (above_ma20 if breakout_relaxed
                               else ind["cross_up_ma20"])
                should_buy = (
                    breakout_ok
                    and bullish
                    and ind["momentum"] > momentum_threshold
                    and (volume_ratio is None
                         or volume_ratio >= cfg.get("min_volume_ratio", 1.1))
                )
            elif signal_logic == "factor_rank":
                # 多因子横截面排序（借鉴 Qlib 因子模型思想，纯 stdlib 实现）。
                # 与事件触发型策略正交：不赌某个"穿越/突破"事件，而是先把
                # 满足基本门槛(多头 + 动量达标 + 未超买)的票全部入围，再用
                # 可配置权重的合成因子分在候选集里择优买入"相对最强"者。
                rsi14 = ind.get("rsi14")
                not_overbought = rsi14 is None or rsi14 <= cfg.get("rsi_overbought", 72)
                should_buy = (
                    bullish
                    and ind["momentum"] > momentum_threshold
                    and not_overbought
                )
                if should_buy:
                    weights = cfg.get("factor_weights", {})
                    w_mom = weights.get("momentum", 1.0)
                    w_vol = weights.get("volume", 0.3)
                    w_rsi = weights.get("rsi_mid", 0.2)
                    w_dist = weights.get("distance_penalty", 0.5)
                    volume_ratio = ind.get("volume_ratio")
                    # 量能贡献：相对成交量越高越好，封顶 2.0 归一化。
                    vol_c = (min(volume_ratio, 2.0) / 2.0) if volume_ratio else 0.0
                    # RSI 适中度：偏离 55 越远得分越低（惩罚超买/超卖两端）。
                    rsi_mid = 1.0 - min(abs((rsi14 if rsi14 is not None else 55) - 55) / 45.0, 1.0)
                    # 偏离度惩罚：离 MA20 越远越像追高。
                    dist = (ind["close"] / ind["ma20"] - 1) if ind.get("ma20") else 0.0
                    ind["factor_score"] = (
                        w_mom * ind["momentum"]
                        + w_vol * vol_c
                        + w_rsi * rsi_mid
                        - w_dist * max(dist, 0.0)
                    )

            # 突破确认：拒绝远离 MA20 的追高信号；非标准数据不阻塞既有策略。
            if should_buy and ind.get("ma20") and ind.get("close"):
                distance = ind["close"] / ind["ma20"] - 1
                if distance > max_breakout_distance:
                    should_buy = False
            if should_buy:
                candidates.append(ind)

        candidates.sort(key=lambda x: x.get("factor_score", x["momentum"]), reverse=True)
        # 估算总资产用于仓位控制
        from astock.core.rules import market_value
        _, total = market_value(st, quotes)
        budget_per = total * max_weight
        for ind in candidates[:min(slots, effective_max_new)]:
            q = quotes.get(ind["code"]) or {}
            px = q.get("price") or ind["close"]
            if px <= 0:
                continue
            qty = int(budget_per / px // 100 * 100)
            if qty >= 100:
                signals.append({"action": "buy", "code": ind["code"], "qty": qty,
                                "reason": f"{signal_logic} 动量{ind['momentum']*100:.1f}%"})
    return signals
