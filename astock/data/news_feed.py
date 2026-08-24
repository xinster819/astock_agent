"""
个股新闻取数 —— 给 C组(多空辩论)/D组(新闻情绪) 注入【真实、带时效】的新闻，
根治"输入无新闻字段→agent 凭训练记忆编新闻"的幻觉根因。

设计原则（对齐 market_regime 的分级异常处置）：
  1) 只输出【可验证】的新闻：每条必带 发布时间 + 来源 + 链接；无时间的一律 stale。
  2) 强制时效标注：超 stale_days（默认 3 天，约 T-3）判 stale=True，
     让上层能执行"新闻须新鲜+带源才可作唯一买入依据"的闸门。
  3) 取数失败不抛、不静默：返回 status=unavailable + reason，绝不凭空 risk_off，
     也绝不让 agent 误以为"无消息=利好"。
  4) fetch 可注入：默认走 akshare stock_news_em，但为可测/可绕过 akshare 版本 bug，
     允许调用方传入自定义 fetch(code)->DataFrame。

对外入口：
  get_stock_news(code, ...)        单票
  get_news_for_codes(codes, ...)   批量（去重 + 票数上限 + 单票失败隔离）
纯 stdlib（akshare 仅在默认 fetch 内延迟 import）。
"""
import datetime as dt

DEFAULT_STALE_DAYS = 3      # 超过约 T-3 视为过期，不能作唯一买入依据
DEFAULT_MAX_ITEMS = 5       # 每票最多保留几条，控制决策包体积
DEFAULT_MAX_CODES = 10      # 批量最多拉几只，控制网络请求数/限频风险


def _default_fetch(code):
    """默认真源：akshare 个股新闻。绕开该版本 stock_news_em 在
    pandas/pyarrow 上的 str.replace 崩溃（强制 python 字符串存储）。"""
    import pandas as pd
    old = pd.get_option("mode.string_storage")
    try:
        pd.set_option("mode.string_storage", "python")
        import akshare as ak
        return ak.stock_news_em(symbol=code)
    finally:
        try:
            pd.set_option("mode.string_storage", old)
        except Exception:
            pass


def _parse_time(raw):
    """解析发布时间。成功返回 datetime，失败返回 None（判 stale，不抛）。"""
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _row_get(row, *keys):
    """从 DataFrame 行(dict-like)里取第一个存在且非空的字段。"""
    for k in keys:
        try:
            v = row.get(k)
        except AttributeError:
            v = row[k] if k in row else None
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def get_stock_news(code, max_items=DEFAULT_MAX_ITEMS, stale_days=DEFAULT_STALE_DAYS,
                   now=None, fetch=None):
    """拉单票新闻，结构化 + 时效标注。永不抛异常。

    返回 dict：
      status: ok | stale_only | empty | unavailable
      items:  [{title, source, url, published, age_days, stale}, ...]
      fresh_count: 新鲜(非 stale)条数
      reason: 仅 unavailable 时给失败原因
    """
    now = now or dt.datetime.now()
    fetch = fetch or _default_fetch

    try:
        df = fetch(code)
    except Exception as e:
        return {"status": "unavailable", "items": [], "fresh_count": 0,
                "reason": repr(e)[:120]}

    if df is None or len(df) == 0:
        return {"status": "empty", "items": [], "fresh_count": 0}

    items = []
    for _, row in df.head(max_items).iterrows():
        title = _row_get(row, "新闻标题", "标题", "title")
        source = _row_get(row, "文章来源", "来源", "source")
        url = _row_get(row, "新闻链接", "链接", "url")
        raw_time = _row_get(row, "发布时间", "时间", "datetime")
        pub_dt = _parse_time(raw_time) if raw_time else None
        if pub_dt is None:
            published, age_days, stale = None, None, True
        else:
            age_days = (now.date() - pub_dt.date()).days
            stale = age_days > stale_days or age_days < 0
            published = pub_dt.strftime("%Y-%m-%d %H:%M")
        items.append({"title": title, "source": source, "url": url,
                      "published": published, "age_days": age_days, "stale": stale})

    fresh = sum(1 for it in items if not it["stale"])
    status = "ok" if fresh > 0 else "stale_only"
    return {"status": status, "items": items, "fresh_count": fresh}


def get_news_for_codes(codes, max_codes=DEFAULT_MAX_CODES, max_items=DEFAULT_MAX_ITEMS,
                       stale_days=DEFAULT_STALE_DAYS, now=None, fetch=None):
    """批量取新闻：去重、限票数、单票失败隔离（不拖垮整体）。

    返回 {code: <get_stock_news 结果>}。只对前 max_codes 只（去重后、保序）取数，
    控制网络请求数，避免全池 50 只逐票请求被限频。
    """
    now = now or dt.datetime.now()
    seen, uniq = set(), []
    for c in codes:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
        if len(uniq) >= max_codes:
            break

    out = {}
    for c in uniq:
        out[c] = get_stock_news(c, max_items=max_items, stale_days=stale_days,
                                now=now, fetch=fetch)
    return out


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "002594"
    r = get_stock_news(code)
    print(f"== {code} 新闻体检 == status={r['status']} fresh={r.get('fresh_count')}")
    for it in r["items"]:
        flag = "⚠stale" if it["stale"] else "fresh"
        print(f"  [{flag}] {it['published']} | {it['source']} | {it['title']}")
