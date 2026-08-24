"""
行情层：实时快照 + 日线历史。
数据源优先级：东方财富 push2 实时接口 -> akshare 兜底。
全部为只读 HTTP 请求，不起任何服务。
"""
import time
import json
import urllib.request
import datetime as dt

from astock.runtime import clock as market_time

EM_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170,f168"
# f43现价(*100? 实际带小数位f59) 这里改用更稳的字段, 见解析


def _http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _secid(code: str) -> str:
    """沪市1. 深市0.  6开头/科创688/上证指数 -> 1 ; 其余 -> 0"""
    code = code.strip()
    if code.startswith(("6", "5", "11", "900")):  # 沪市股票/ETF/可转债/B股
        return f"1.{code}"
    return f"0.{code}"


def _sanity_check(q):
    """价格合理性校验：防脏数据成交。
    返回 (ok, reason)。现价为0视为'无报价'（休市/拉取失败），交给下单层拒单，不算脏数据。"""
    px = q.get("price", 0)
    if px <= 0:
        return True, "no_quote"  # 0 由下单层按'无有效现价'拒单
    lu, ld, pc = q.get("limit_up", 0), q.get("limit_down", 0), q.get("prev_close", 0)
    # 1) 现价必须落在涨跌停带内（留0.5%容差，防边界四舍五入）
    if lu > 0 and px > lu * 1.005:
        return False, f"现价{px}高于涨停{lu}，疑似脏数据"
    if ld > 0 and px < ld * 0.995:
        return False, f"现价{px}低于跌停{ld}，疑似脏数据"
    # 2) 现价相对昨收偏离不应超过 ±21%（容纳20cm涨跌停板+容差）
    if pc > 0 and abs(px / pc - 1) > 0.21:
        return False, f"现价{px}较昨收{pc}偏离>21%，疑似脏数据"
    return True, "ok"


def _get_quote_em(code: str, retries=3, retry_wait=0.6):
    """
    东方财富单源快照（带重试 + 价格合理性校验）。供 get_quote 交叉验证用。
    - 网络/解析异常：重试 retries 次，仍失败 -> price=0.0 + error（下单层会拒单）。
    - 拉到的价格若未通过 _sanity_check：price 置0 + dirty 标记（下单层拒单），绝不用脏价成交。
    现价为0统一表示'本轮该票不可交易'，是安全失败。
    """
    secid = _secid(code)
    # f43现价 f44最高 f45最低 f46今开 f60昨收 f47成交量 f48成交额 f57代码 f58名称
    # f51涨停 f52跌停  注意东财价格放大100倍(2位小数)或1000倍, 用f59小数位修正
    url = (f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f44,f45,f46,f47,f48,f51,f52,f57,f58,f59,f60,f169,f170")
    last_err = ""
    for attempt in range(retries):
        try:
            raw = _http_get(url)
            d = json.loads(raw).get("data") or {}
            if not d:
                raise ValueError("empty data")
            scale = 10 ** int(d.get("f59", 2) or 2)
            def px(v):
                try:
                    return round(float(v) / scale, 4) if v not in (None, "-", "") else 0.0
                except Exception:
                    return 0.0
            q = {
                "code": d.get("f57", code),
                "name": d.get("f58", ""),
                "price": px(d.get("f43")),
                "high": px(d.get("f44")),
                "low": px(d.get("f45")),
                "open": px(d.get("f46")),
                "limit_up": px(d.get("f51")),
                "limit_down": px(d.get("f52")),
                "prev_close": px(d.get("f60")),
                "vol": d.get("f47", 0),
                "amount": d.get("f48", 0),
                "ts": market_time.stamp(),
                "attempts": attempt + 1,
            }
            ok, reason = _sanity_check(q)
            if not ok:
                # 脏数据：本次重试，最后一次仍脏则置0拒单
                last_err = reason
                if attempt < retries - 1:
                    time.sleep(retry_wait)
                    continue
                q["price"] = 0.0
                q["dirty"] = reason
            return q
        except Exception as e:
            last_err = repr(e)[:120]
            if attempt < retries - 1:
                time.sleep(retry_wait)
                continue
    return {"code": code, "name": "", "price": 0.0, "error": last_err,
            "attempts": retries,
            "ts": market_time.stamp()}


# ---- 多源交叉验证开关与阈值 ----
CROSS_VALIDATE = True
DIVERGE_TOL = 0.005   # 多源现价偏差容忍度 0.5%

import os
SPREAD_LOG = os.path.join(os.path.dirname(__file__), "spread_log.csv")


def log_spread(quotes):
    """记录每只票的多源现价与极差，供收盘后校准阈值。只记 >=2 个有效源的票。"""
    new = not os.path.exists(SPREAD_LOG)
    rows = []
    for code, q in quotes.items():
        srcs = q.get("sources") or {}
        vals = [v for v in srcs.values() if isinstance(v, (int, float)) and v > 0]
        if len(vals) < 2:
            continue
        spread = (max(vals) - min(vals)) / (sum(vals) / len(vals))
        rows.append((q.get("ts", ""), code, len(vals),
                     round(spread * 100, 4), q.get("cross", ""),
                     ";".join(f"{k}={v}" for k, v in srcs.items())))
    if not rows:
        return
    with open(SPREAD_LOG, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,代码,有效源数,价差%,判定,各源价\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


def calibrate_tol(percentile=99):
    """读 spread_log，给出价差分布与建议阈值（默认取99分位+缓冲）。返回 dict。"""
    if not os.path.exists(SPREAD_LOG):
        return {"error": "暂无价差日志（尚未交易时段实跑过）"}
    spreads = []
    with open(SPREAD_LOG, "r", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 4:
                try:
                    spreads.append(float(parts[3]))
                except Exception:
                    pass
    if not spreads:
        return {"error": "价差日志为空"}
    spreads.sort()
    n = len(spreads)
    def pct(p):
        return spreads[min(n - 1, int(n * p / 100))]
    p99 = pct(percentile)
    suggest = round((p99 * 1.5) / 100, 5)  # 99分位*1.5倍缓冲，转回小数
    return {
        "样本数": n, "最小价差%": spreads[0], "中位价差%": pct(50),
        "p95%": pct(95), "p99%": p99, "最大价差%": spreads[-1],
        "当前阈值%": DIVERGE_TOL * 100,
        "建议阈值%": round(suggest * 100, 4),
        "建议DIVERGE_TOL": suggest,
        "说明": "建议阈值=99分位价差×1.5倍缓冲；若建议值远小于当前0.5%，说明阈值过松可收紧；远大于则当前过紧易误拒。",
    }


def load_sample_pool():
    """采样池：用于价差交叉验证统计的大票池（默认沪深300，sample_pool.json）。
    与交易池(watchlist)解耦——交易仍小而精，采样尽可能多以充分校准阈值。"""
    p = os.path.join(os.path.dirname(__file__), "sample_pool.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("pool", [])
    return []


def sample_spreads(codes=None):
    """
    批量双源(新浪+腾讯)采集采样池现价，统计价差并写入 spread_log.csv。
    返回 (有效双源数, 写入行数)。仅做统计校准，不参与下单。
    几百只票约1秒，远快于逐只三源。
    """
    if codes is None:
        codes = load_sample_pool()
    if not codes:
        return 0, 0
    try:
        from astock.data import quote_sources as qs
    except Exception:
        return 0, 0
    res = qs.fetch_all_batch(codes)
    ts = market_time.stamp()
    rows = []
    for code, v in res.items():
        vals = [x for x in (v.get("sina", 0), v.get("tencent", 0))
                if isinstance(x, (int, float)) and x > 0]
        if len(vals) < 2:
            continue
        spread = (max(vals) - min(vals)) / (sum(vals) / len(vals))
        rows.append((ts, code, len(vals), round(spread * 100, 4), "sample",
                     f"sina={v.get('sina')};tencent={v.get('tencent')}"))
    if not rows:
        return 0, 0
    new = not os.path.exists(SPREAD_LOG)
    with open(SPREAD_LOG, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,代码,有效源数,价差%,判定,各源价\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return len(rows), len(rows)


def get_quote(code: str):
    """
    对外统一入口：多源交叉验证版。
    机制：
      - 采集 东方财富/新浪/腾讯 三源（quote_sources），取各源 price>0 的有效现价。
      - 有效源 >=2 且 极差/均值 <= DIVERGE_TOL：通过，price 取中位数；附 cross='agree'。
      - 有效源 >=2 但偏差 > DIVERGE_TOL：分歧，price=0 + diverge（下单层拒单），附两源价。
      - 有效源 ==1：降级放行，price 用该源，附 cross='single_source'（已知风险，仍记录）。
      - 有效源 ==0：price=0（休市/全失败），下单层拒单。
    涨跌停/昨收等元数据优先取东方财富（字段最全），缺失再用腾讯。
    CROSS_VALIDATE=False 时退回纯东方财富单源（_get_quote_em）。
    """
    if not CROSS_VALIDATE:
        return _get_quote_em(code)
    try:
        from astock.data import quote_sources as qs
    except Exception:
        return _get_quote_em(code)

    srcs = qs.fetch_all(code)
    ts = market_time.stamp()
    # 元数据底座：优先东财，其次腾讯
    base = None
    for pref in ("eastmoney", "tencent", "sina"):
        s = srcs.get(pref)
        if s and not s.get("error"):
            base = s
            break
    name = (base or {}).get("name", "")
    # 涨跌停/昨收：优先有非0值的源
    def pick(field):
        for pref in ("eastmoney", "tencent", "sina"):
            s = srcs.get(pref) or {}
            if s.get(field, 0):
                return s[field]
        return 0.0

    out = {
        "code": code, "name": name,
        "limit_up": pick("limit_up"), "limit_down": pick("limit_down"),
        "prev_close": pick("prev_close"),
        "open": pick("open"), "high": pick("high"), "low": pick("low"),
        "ts": ts, "sources": {k: v.get("price", 0) if not v.get("error") else "ERR"
                              for k, v in srcs.items()},
    }

    valid = {k: v["price"] for k, v in srcs.items()
             if not v.get("error") and v.get("price", 0) > 0}

    if len(valid) == 0:
        out["price"] = 0.0
        out["cross"] = "no_quote"
        return out

    prices = sorted(valid.values())
    if len(valid) >= 2:
        spread = (prices[-1] - prices[0]) / (sum(prices) / len(prices))
        if spread <= DIVERGE_TOL:
            mid = prices[len(prices) // 2]  # 中位数，抗单源异常
            out["price"] = round(mid, 4)
            out["cross"] = f"agree({len(valid)}源, 偏差{spread*100:.2f}%)"
        else:
            out["price"] = 0.0
            out["cross"] = "diverge"
            out["diverge"] = f"多源分歧>{DIVERGE_TOL*100:.1f}%: {valid}"
        return out

    # 只有1个有效源：降级放行但标记
    only = list(valid.items())[0]
    out["price"] = round(only[1], 4)
    out["cross"] = f"single_source({only[0]})"
    return out


def get_quotes(codes):
    out = {}
    for c in codes:
        out[c] = get_quote(c)
        time.sleep(0.15)  # 轻微限速，避免被封
    return out


def get_hist(code: str, start: str, end: str, adjust="qfq"):
    """个股日线历史。**多源兜底**，返回标准化 DataFrame（含"日期"/"收盘"/"成交量"）。

    注意：本函数只服务【个股】。指数（000300/000001/399xxx/科创50 等）请用
    get_index_hist —— 个股接口 stock_zh_a_hist 对指数代码会返回空体并抛
    JSONDecodeError，历史上曾导致 regime 计算失败→静默兜底 risk_off。

    源优先级（与 get_index_hist 对齐）：
      1) akshare stock_zh_a_hist（东财源，字段最全）
      2) akshare stock_zh_a_daily（新浪源）
      3) 腾讯日线 qt.gtimg.cn（纯 HTTP 兜底，不依赖 akshare 可用性）

    【为什么必须多源】2026-08-23 实测：东财 push2/push2his 整站 502，
    stock_zh_a_hist 直接 ConnectionError。而本函数原先只有这一条路，一挂则
    strategy._indicators() 全部返回 None → 所有策略静默不开仓、且无任何告警
    ——与 2026-07-31 时区停摆同属"静默失效"家族。故补齐兜底。

    成交量单位各源不同（手/股），但 volume_ratio 是【同一序列内】末根与20日均值
    之比，单位可约掉；只要不跨源拼接同一条序列即可，本函数每次只返回单一源。
    """
    import pandas as pd
    errors = []

    # 源1：东财（akshare stock_zh_a_hist）—— 字段最全，健康时优先
    # 带熔断：东财整站 502 时不再逐只白等约 2s（全池 50 只 × 13 账户 = 几十分钟）
    from astock.data import source_health
    _health = source_health.QUOTES
    if _health.should_skip("eastmoney_hist"):
        errors.append("em:circuit_open(近期连续失败，冷却中)")
    else:
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=start, end_date=end, adjust=adjust)
            if df is not None and len(df):
                _health.record_ok("eastmoney_hist")
                return df
            _health.record_fail("eastmoney_hist")
            errors.append("em:空结果")
        except Exception as e:
            _health.record_fail("eastmoney_hist")
            errors.append(f"em:{repr(e)[:60]}")

    # 源2：新浪（akshare stock_zh_a_daily）—— 全量历史，需列名标准化+按日期裁剪
    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=_stock_symbol(code), adjust=adjust)
        if df is not None and len(df):
            out = pd.DataFrame({
                "日期": df["date"].astype(str),
                "开盘": df["open"].astype(float),
                "收盘": df["close"].astype(float),
                "最高": df["high"].astype(float),
                "最低": df["low"].astype(float),
                "成交量": df["volume"].astype(float),
            })
            out = _clip_dates(out, start, end)
            if len(out):
                return out.reset_index(drop=True)
    except Exception as e:
        errors.append(f"sina:{repr(e)[:60]}")

    # 源3：腾讯日线（纯 HTTP）—— akshare 整体不可用时的最后一道
    try:
        sym = _stock_symbol(code)
        fq = "qfq" if adjust == "qfq" else ("hfq" if adjust == "hfq" else "")
        url = (f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,,,640,{fq}")
        obj = json.loads(_http_get(url))
        node = obj["data"][sym]
        kline = node.get(f"{fq}day") or node.get("day") or node.get("qfqday") or []
        # 腾讯格式：[日期, 开, 收, 高, 低, 成交量]
        rows = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in kline if len(r) >= 6]
        if rows:
            out = pd.DataFrame(rows, columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
            out = _clip_dates(out, start, end)
            if len(out):
                return out.reset_index(drop=True)
    except Exception as e:
        errors.append(f"tencent:{repr(e)[:60]}")

    raise RuntimeError(f"get_hist({code}) 全部源失败: " + " | ".join(errors))


def _clip_dates(df, start, end):
    """按 YYYYMMDD / YYYY-MM-DD 起止裁剪标准化后的日线表。"""
    if start:
        df = df[df["日期"] >= _fmt_date(start)]
    if end:
        df = df[df["日期"] <= _fmt_date(end)]
    return df


def _stock_symbol(code: str) -> str:
    """把 6 位【个股】代码映射成带交易所前缀的 symbol（sh/sz）。

    ⚠ 不要用 _index_symbol 代替：那是【指数】映射，把 000xxx 一律当上证系列判为 sh。
    个股 000001 是平安银行(sz000001)，而 sh000001 是上证指数 —— 用错会让整条
    日线序列变成指数行情。口径与 _secid / quote_sources._prefix 一致：
    6/5/11/900 开头为沪市，其余为深市。
    """
    code = code.strip()
    if code.startswith(("sh", "sz")):
        return code
    return ("sh" if code.startswith(("6", "5", "11", "900")) else "sz") + code


def _index_symbol(code: str) -> str:
    """把 6 位指数代码映射成带交易所前缀的 symbol（sh/sz）。
    上证系列(000xxx/688)→sh；深证/创业板(399xxx)→sz。"""
    code = code.strip()
    if code.startswith(("sh", "sz")):
        return code
    if code.startswith("399"):
        return "sz" + code
    # 000xxx(沪深300/上证综指/科创等)、688、其余上证系列
    return "sh" + code


def get_index_hist(code: str, start: str = None, end: str = None):
    """指数日线历史。多源兜底，返回标准化 DataFrame（含"日期"、"收盘"列）。

    源优先级：
      1) akshare stock_zh_index_daily（新浪源，稳定，无需日期）
      2) akshare index_zh_a_hist（东财源，可按日期，偶发 502）
      3) 腾讯日线 qt.gtimg.cn（纯 HTTP 兜底，不依赖 akshare 可用性）
    任一源成功即返回；全部失败抛最后一个异常，由调用方决定降级策略。
    """
    import pandas as pd
    sym = _index_symbol(code)          # e.g. sh000300
    bare = sym[2:]                     # e.g. 000300
    errors = []

    # 源1：新浪（stock_zh_index_daily）——最稳，全量日线
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=sym)
        if df is not None and len(df):
            out = pd.DataFrame({"日期": df["date"].astype(str), "收盘": df["close"].astype(float)})
            if start:
                out = out[out["日期"] >= _fmt_date(start)]
            if end:
                out = out[out["日期"] <= _fmt_date(end)]
            if len(out):
                return out.reset_index(drop=True)
    except Exception as e:
        errors.append(f"sina:{repr(e)[:60]}")

    # 源2：东财（index_zh_a_hist）——按日期，偶发 502
    try:
        import akshare as ak
        df = ak.index_zh_a_hist(symbol=bare, period="daily",
                                start_date=(start or "20200101"),
                                end_date=(end or dt.datetime.now().strftime("%Y%m%d")))
        if df is not None and len(df):
            return pd.DataFrame({"日期": df["日期"].astype(str), "收盘": df["收盘"].astype(float)}).reset_index(drop=True)
    except Exception as e:
        errors.append(f"em:{repr(e)[:60]}")

    # 源3：腾讯日线兜底（纯 HTTP）
    try:
        url = (f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,,,320,qfq")
        raw = _http_get(url)
        obj = json.loads(raw)
        node = obj["data"][sym]
        kline = node.get("day") or node.get("qfqday") or []
        rows = [(r[0], float(r[2])) for r in kline if len(r) >= 3]
        if rows:
            out = pd.DataFrame(rows, columns=["日期", "收盘"])
            if start:
                out = out[out["日期"] >= _fmt_date(start)]
            if end:
                out = out[out["日期"] <= _fmt_date(end)]
            if len(out):
                return out.reset_index(drop=True)
    except Exception as e:
        errors.append(f"tencent:{repr(e)[:60]}")

    raise RuntimeError("get_index_hist 全部源失败: " + " | ".join(errors))


def _fmt_date(s: str) -> str:
    """YYYYMMDD 或 YYYY-MM-DD → YYYY-MM-DD，便于与标准化日期列比较。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


# ---- 交易时段判断（A股：9:30-11:30, 13:00-15:00，周一至周五，未含法定节假日表）----
def is_trading_now(now=None):
    """判断当前是否交易时段。**判定始终以交易所时区(Asia/Shanghai)为准。**

    ⚠ 2026-07-31 停摆事故的根因就在这一行：旧实现是 `now or dt.datetime.now()`，
    直接拿【进程本地时间】去比 9:30-15:00。当进程 TZ 是 UTC 时，北京 14:00 被
    读成 06:00 → "未开盘" → 12 个账户连续三周零成交，且无任何告警。
    修复要点：调用方(run/run_exp/execute/prepare)传进来的 now 是朴素的进程本地
    时间，必须一并归一 —— 只改本函数的默认取值是修不好的。
    """
    now = market_time.to_market(now)
    if now.weekday() >= 5:
        return False, "周末休市"
    t = now.time()
    am = dt.time(9, 30) <= t <= dt.time(11, 30)
    pm = dt.time(13, 0) <= t <= dt.time(15, 0)
    if am or pm:
        return True, "交易中"
    if t < dt.time(9, 30):
        return False, "未开盘"
    if dt.time(11, 30) < t < dt.time(13, 0):
        return False, "午间休市"
    return False, "已收盘"


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(json.dumps(get_quote(code), ensure_ascii=False, indent=2))
    print(is_trading_now())
