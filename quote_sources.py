"""
多源行情采集：东方财富 / 新浪 / 腾讯，三个相互独立的免费源。
每个源返回统一结构 {price, prev_close, limit_up, limit_down, open, high, low}，
取不到的字段为 0。供 market.py 做交叉验证。
"""
import json
import urllib.request


def _get(url, timeout=8, referer=None, gbk=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("gbk", "ignore") if gbk else raw.decode("utf-8", "ignore")


def _prefix(code):
    """沪市 sh / 深市 sz 前缀判断。"""
    return "sh" if code.startswith(("6", "5", "11", "900")) else "sz"


# ---------- 源1：东方财富 ----------
def from_eastmoney(code):
    secid = ("1." if _prefix(code) == "sh" else "0.") + code
    url = (f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f44,f45,f46,f47,f48,f51,f52,f57,f58,f59,f60")
    d = json.loads(_get(url)).get("data") or {}
    if not d:
        raise ValueError("eastmoney empty")
    scale = 10 ** int(d.get("f59", 2) or 2)
    def px(v):
        try:
            return round(float(v) / scale, 4) if v not in (None, "-", "") else 0.0
        except Exception:
            return 0.0
    return {
        "source": "eastmoney", "name": d.get("f58", ""),
        "price": px(d.get("f43")), "open": px(d.get("f46")),
        "high": px(d.get("f44")), "low": px(d.get("f45")),
        "limit_up": px(d.get("f51")), "limit_down": px(d.get("f52")),
        "prev_close": px(d.get("f60")),
    }


# ---------- 源2：新浪 ----------
def from_sina(code):
    sym = _prefix(code) + code
    url = f"http://hq.sinajs.cn/list={sym}"
    txt = _get(url, referer="https://finance.sina.com.cn", gbk=True)
    # var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,...";
    inner = txt.split('"')[1]
    f = inner.split(",")
    if len(f) < 6:
        raise ValueError("sina malformed")
    def fl(i):
        try:
            return round(float(f[i]), 4)
        except Exception:
            return 0.0
    return {
        "source": "sina", "name": f[0],
        "open": fl(1), "prev_close": fl(2), "price": fl(3),
        "high": fl(4), "low": fl(5),
        "limit_up": 0.0, "limit_down": 0.0,  # 新浪不直接给涨跌停
    }


# ---------- 源3：腾讯 ----------
def from_tencent(code):
    sym = _prefix(code) + code
    url = f"http://qt.gtimg.cn/q={sym}"
    txt = _get(url, gbk=True)
    # v_sh600519="1~名称~代码~现价~昨收~今开~...~涨停(idx47)~跌停(idx48)~...";
    inner = txt.split('"')[1]
    f = inner.split("~")
    if len(f) < 6:
        raise ValueError("tencent malformed")
    def fl(i):
        try:
            return round(float(f[i]), 4)
        except Exception:
            return 0.0
    out = {
        "source": "tencent", "name": f[1],
        "price": fl(3), "prev_close": fl(4), "open": fl(5),
        "high": fl(33), "low": fl(34),
        "limit_up": 0.0, "limit_down": 0.0,
    }
    # 腾讯长字段里 idx47/48 常为涨停/跌停
    if len(f) > 48:
        out["limit_up"], out["limit_down"] = fl(47), fl(48)
    return out


SOURCES = {"eastmoney": from_eastmoney, "sina": from_sina, "tencent": from_tencent}


# ---------- 批量采集：新浪 / 腾讯（逗号串一次取多只）----------
# 仅用于"价差采样池"做交叉验证统计，不参与下单（下单仍走逐只三源 fetch_all）。
# 两源相互独立且都支持一次请求多只代码，几百只票分批即可，远快于逐只。
def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def batch_sina(codes, chunk=120):
    """新浪批量现价。返回 {code: price}（取不到/停牌为0）。"""
    out = {}
    for grp in _chunks(codes, chunk):
        syms = ",".join(_prefix(c) + c for c in grp)
        try:
            txt = _get("http://hq.sinajs.cn/list=" + syms,
                       referer="https://finance.sina.com.cn", gbk=True)
        except Exception:
            continue
        for line in txt.strip().split("\n"):
            if 'hq_str_' not in line or '"' not in line:
                continue
            sym = line.split("hq_str_")[1].split("=")[0]          # sh600519
            code = sym[2:]
            f = line.split('"')[1].split(",")
            try:
                out[code] = round(float(f[3]), 4) if len(f) > 3 else 0.0
            except Exception:
                out[code] = 0.0
    return out


def batch_tencent(codes, chunk=60):
    """腾讯批量现价。返回 {code: price}（取不到/停牌为0）。"""
    out = {}
    for grp in _chunks(codes, chunk):
        syms = ",".join(_prefix(c) + c for c in grp)
        try:
            txt = _get("http://qt.gtimg.cn/q=" + syms, gbk=True)
        except Exception:
            continue
        for line in txt.strip().split("\n"):
            if '="' not in line:
                continue
            sym = line.split("_")[1].split("=")[0] if "_" in line else ""
            code = sym[2:] if len(sym) > 2 else ""
            f = line.split('"')[1].split("~")
            if not code or len(f) < 4:
                continue
            try:
                out[code] = round(float(f[3]), 4)
            except Exception:
                out[code] = 0.0
    return out


def fetch_all_batch(codes):
    """
    批量双源（新浪+腾讯）现价采集，用于价差采样统计。
    返回 {code: {"sina": px, "tencent": px}}，缺失为0。
    比逐只 fetch_all 快 ~100倍，适合几百只采样池。
    """
    sina = batch_sina(codes)
    tx = batch_tencent(codes)
    return {c: {"sina": sina.get(c, 0.0), "tencent": tx.get(c, 0.0)} for c in codes}


def fetch_all(code, retries=2, retry_wait=0.5):
    """采集所有源，返回 {source_name: quote_dict 或 {'error':...}}。每源带重试。

    带健康度熔断：连续失败的源在冷却期内直接跳过，不再逐只白等重试。
    熔断的源在结果里仍以 error 形式出现（标记 circuit_open），
    对交叉验证的贡献与"取数失败"完全一致——不改判定，只省延迟。
    """
    import time
    import source_health
    health = source_health.QUOTES
    results = {}
    for name, fn in SOURCES.items():
        if health.should_skip(name):
            results[name] = {"source": name, "error": "circuit_open(近期连续失败，冷却中)",
                             "circuit_open": True}
            continue
        for attempt in range(retries):
            try:
                results[name] = fn(code)
                health.record_ok(name)
                break
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(retry_wait)
                else:
                    results[name] = {"source": name, "error": repr(e)[:100]}
                    health.record_fail(name)
    return results


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(json.dumps(fetch_all(code), ensure_ascii=False, indent=2))
