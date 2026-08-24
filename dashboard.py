"""
观测站生成器：读取全部账户(A组主账户 / exp1-8 / B组)的 state+equity+trades，
生成时拉一次实时行情算个股浮盈，渲染成一个【自包含单页 HTML】(数据内嵌 + Chart.js)，
供随时打开观察各组炒股进展、持仓、交易记录、整体收益与个股收益。

设计：
  - 沙箱禁止常驻网络服务，故产物是静态 HTML，不依赖后端，双击即可看。
  - 数据每天变 -> 本脚本可随时重跑刷新；也可挂定时任务自动重生成并上传。
  - 个股收益 = 已实现盈亏(卖出记录备注里的"盈亏")+ 未实现浮盈(现价 vs 成本)。
用法：python3 dashboard.py            # 生成 dashboard.html
      python3 dashboard.py --no-live  # 跳过实时行情(用成本价占位，快速出图)
"""
import os
import sys
import re
import json
import csv
import datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "dashboard.html")

# 账户清单： (展示名, 策略说明, state路径, equity路径, trades路径, 颜色)
ACCOUNTS = [
    ("A组·纯规则对照", "基准对照组(纯规则自动交易)",
     "state.json", "equity.csv", "trades.csv", "#64748b"),
    ("exp1·基准策略", "原始均线趋势跟踪",
     "experiments/exp1_state.json", "experiments/exp1_equity.csv", "experiments/exp1_trades.csv", "#3b82f6"),
    ("exp2·放宽买入", "降低买入门槛，更快入场",
     "experiments/exp2_state.json", "experiments/exp2_equity.csv", "experiments/exp2_trades.csv", "#22c55e"),
    ("exp3·严格趋势", "只抓强趋势，提高胜率",
     "experiments/exp3_state.json", "experiments/exp3_equity.csv", "experiments/exp3_trades.csv", "#f59e0b"),
    ("exp4·金叉策略", "MA5上穿MA20金叉买入",
     "experiments/exp4_state.json", "experiments/exp4_equity.csv", "experiments/exp4_trades.csv", "#ef4444"),
    ("exp5·纯动量", "趋势与量能确认的动量策略",
     "experiments/exp5_state.json", "experiments/exp5_equity.csv", "experiments/exp5_trades.csv", "#a855f7"),
    ("exp6·状态适配趋势", "仅在正常市场环境运行的低仓位趋势策略",
     "experiments/exp6_state.json", "experiments/exp6_equity.csv", "experiments/exp6_trades.csv", "#06b6d4"),
    ("exp7·均值回归", "中期趋势内的超卖反弹策略",
     "experiments/exp7_state.json", "experiments/exp7_equity.csv", "experiments/exp7_trades.csv", "#84cc16"),
    ("exp8·质量突破", "趋势、突破与成交量确认策略",
     "experiments/exp8_state.json", "experiments/exp8_equity.csv", "experiments/exp8_trades.csv", "#f97316"),
    ("exp9·多因子排序", "门槛入围后按合成因子分择强的横截面选股策略",
     "experiments/exp9_state.json", "experiments/exp9_equity.csv", "experiments/exp9_trades.csv", "#14b8a6"),
    ("B组·Agent决策", "规则做护栏，agent做最终买卖判断",
     "groupB/state.json", "groupB/equity.csv", "groupB/trades.csv", "#ec4899"),
    ("C组·多空辩论", "规则做护栏，多智能体多空辩论后做最终买卖判断",
     "groupC/state.json", "groupC/equity.csv", "groupC/trades.csv", "#8b5cf6"),
    ("D组·新闻情绪", "规则做护栏，结合新闻情绪面做最终买卖判断",
     "groupD/state.json", "groupD/equity.csv", "groupD/trades.csv", "#0ea5e9"),
]

# 卖出备注里形如 "盈亏-8733.24" / "盈亏 123.4" 的已实现盈亏
_PNL_RE = re.compile(r"盈亏\s*([+-]?\d+(?:\.\d+)?)")


def _read_state(p):
    fp = os.path.join(BASE, p)
    if not os.path.exists(fp):
        return None
    try:
        return json.load(open(fp, encoding="utf-8"))
    except Exception:
        return None


def _read_csv(p):
    fp = os.path.join(BASE, p)
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _fnum(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def collect_live_prices(need_codes, use_live=True):
    """一次性拉取所有需要的代码的实时现价。返回 ({code:{'price','name'}}, status)。
    非交易时段/取价失败会返回空价，调用方回退到成本价。"""
    prices = {}
    if not use_live or not need_codes:
        return prices, "未取实时价(--no-live)"
    try:
        import market
        quotes = market.get_quotes(sorted(need_codes))
        for c, q in quotes.items():
            px = q.get("price") or 0
            if px and px > 0 and not q.get("error"):
                prices[c] = {"price": float(px), "name": q.get("name", "")}
        status = market.is_trading_now()[1]
        return prices, f"实时({status}，取得{len(prices)}/{len(need_codes)}只)"
    except Exception as e:
        return prices, f"行情不可用({repr(e)[:40]})"


def collect(use_live=True):
    # 先汇总所有账户的持仓代码，一次性取价（避免每组重复请求）
    need_codes = set()
    raw = []
    for name, desc, sp, ep, tp, color in ACCOUNTS:
        st = _read_state(sp)
        eq = _read_csv(ep)
        tr = _read_csv(tp)
        raw.append((name, desc, color, st, eq, tr))
        if st:
            need_codes |= set(st.get("positions", {}).keys())

    live, live_status = collect_live_prices(need_codes, use_live)

    data = []
    for name, desc, color, st, eq, tr in raw:
        if st is None:
            data.append({
                "name": name, "desc": desc, "color": color,
                "exists": False, "init": 1_000_000.0, "cash": 1_000_000.0,
                "mv": 0.0, "total": 1_000_000.0, "ret": 0.0, "round": 0,
                "positions": [], "equity": [], "trades": [],
                "realized": 0.0, "unrealized": 0.0, "win": 0, "loss": 0,
            })
            continue

        init = st.get("init_cash", 1_000_000.0)
        cash = st.get("cash", init)
        pos = st.get("positions", {})

        # ---- 持仓 + 个股未实现浮盈（现价优先，回退成本） ----
        positions = []
        mv_live = 0.0
        for code, p in pos.items():
            qty = p.get("qty", 0)
            cost = p.get("cost", 0)
            lp = live.get(code, {})
            price = lp.get("price") or cost  # 无实时价则用成本(=0浮盈)
            pos_mv = price * qty
            mv_live += pos_mv
            upnl = (price - cost) * qty
            upnl_pct = (price / cost - 1) * 100 if cost else 0.0
            positions.append({
                "code": code, "name": p.get("name", "") or lp.get("name", ""),
                "qty": qty, "available": p.get("available", 0),
                "cost": round(cost, 3), "price": round(price, 3),
                "mv": round(pos_mv, 2),
                "upnl": round(upnl, 2), "upnl_pct": round(upnl_pct, 2),
                "live": bool(lp.get("price")),
            })
        positions.sort(key=lambda x: x["upnl"], reverse=True)

        # 总资产：优先用实时持仓市值；无实时价时回退 equity 末行/成本
        mv_cost = sum(p.get("qty", 0) * p.get("cost", 0) for p in pos.values())
        mv = mv_live if live else mv_cost
        total = cash + mv
        if not live and eq:
            try:
                last = eq[-1]
                mv = _fnum(last.get("持仓市值"), mv_cost)
                total = _fnum(last.get("总资产"), cash + mv_cost)
            except Exception:
                pass
        ret = (total / init - 1) * 100 if init else 0.0
        unrealized = sum(p["upnl"] for p in positions)

        # ---- 已实现盈亏 + 胜负（从卖出记录备注解析） ----
        realized = 0.0
        win = loss = 0
        for r in tr:
            if "卖" in r.get("方向", ""):
                m = _PNL_RE.search(r.get("备注", ""))
                if m:
                    v = _fnum(m.group(1))
                    realized += v
                    if v > 0:
                        win += 1
                    elif v < 0:
                        loss += 1
        realized = round(realized, 2)

        # ---- equity 曲线点 ----
        ecurve = []
        for r in eq:
            try:
                ecurve.append({"t": r.get("时间", ""),
                               "total": _fnum(r.get("总资产")),
                               "ret": _fnum(r.get("累计收益率%"))})
            except Exception:
                continue

        # ---- 最近交易(最多30笔，倒序) ----
        trades = []
        for r in tr[-30:][::-1]:
            note = r.get("备注", "")
            m = _PNL_RE.search(note)
            trades.append({
                "t": r.get("时间", ""), "side": r.get("方向", ""),
                "code": r.get("代码", ""), "name": r.get("名称", ""),
                "price": r.get("价格", ""), "qty": r.get("数量", ""),
                "amount": r.get("成交额", ""),
                "pnl": round(_fnum(m.group(1)), 2) if m else None,
                "note": note,
            })

        data.append({
            "name": name, "desc": desc, "color": color, "exists": True,
            "init": init, "cash": round(cash, 2), "mv": round(mv, 2),
            "total": round(total, 2), "ret": round(ret, 3),
            "round": st.get("round", 0),
            "realized": realized, "unrealized": round(unrealized, 2),
            "win": win, "loss": loss,
            "positions": positions, "equity": ecurve, "trades": trades,
        })
    return data, live_status


def _regime_banner_html():
    """构建市场状态横幅：让'当前为什么开/不开仓'一眼可见。

    直接调用集中化 market_regime 模块的三层降级分类，展示：
      · 当前 regime（normal/high_volatility/risk_off）及其对应的开仓档位；
      · 数据来源(live/cache/cold_start_default)与是否降级；
      · 三项底层指标(20日收益、20日波动、距峰回撤)。
    分类失败也不阻断整张观测站，退化为一条中性提示。
    """
    try:
        import market_regime as _mr
        res = _mr.classify()
        regime = res.regime
        detail = res.metrics if isinstance(res.metrics, dict) else {}
        source = res.source
        degraded = bool(res.degraded)
    except Exception as e:
        return ('<div class="regime regime-unknown"><div class="rg-main">'
                '<span class="rg-dot"></span><b>市场状态：未知</b>'
                f'<span class="rg-sub">分类失败：{repr(e)[:60]}</span></div></div>')

    meta = {
        "normal": ("normal", "🟢", "常态", "正常开仓（不限档）"),
        "high_volatility": ("highvol", "🟡", "高波动", "谨慎开仓（每轮最多 1 只新仓）"),
        "risk_off": ("riskoff", "🔴", "避险", "冻结新开仓（仅允许平仓离场）"),
    }
    cls_key, icon, label, gate = meta.get(
        regime, ("unknown", "⚪", regime, "开仓规则未知"))

    def _pct(key, signed=True):
        v = detail.get(key)
        if isinstance(v, (int, float)):
            return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"
        return "—"

    metrics = (
        f'<span class="rg-metric">20日收益 <b>{_pct("index_return_20d")}</b></span>'
        f'<span class="rg-metric">20日波动 <b>{_pct("volatility_20d", signed=False)}</b></span>'
        f'<span class="rg-metric">距峰回撤 <b>{_pct("drawdown_from_peak")}</b></span>'
    )
    asof = detail.get("asof")
    if asof:
        metrics += f'<span class="rg-metric">截至 <b>{asof}</b></span>'

    src_label = {"live": "实时观测", "cache": "缓存回退",
                 "cold_start_default": "冷启动默认"}.get(source, source)
    if degraded:
        badge = (f'<span class="rg-badge rg-degraded">⚠ 降级值 · {src_label}</span>'
                 '<span class="rg-sub">数据源暂不可用，沿用最近一次可信观测/保守默认，非实时判定</span>')
    else:
        badge = f'<span class="rg-badge rg-live">实时 · {src_label}</span>'

    return (
        f'<div class="regime regime-{cls_key}">'
        f'<div class="rg-main">'
        f'<span class="rg-dot"></span>'
        f'<span class="rg-icon">{icon}</span>'
        f'<b class="rg-label">市场状态：{label}</b>'
        f'<span class="rg-gate">{gate}</span>'
        f'{badge}'
        f'</div>'
        f'<div class="rg-metrics">{metrics}</div>'
        f'</div>'
    )


def render(data, live_status):
    gen_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(data, ensure_ascii=False)
    html = (HTML_TEMPLATE
            .replace("__DATA__", payload)
            .replace("__TS__", gen_ts)
            .replace("__LIVE__", live_status)
            .replace("__REGIME__", _regime_banner_html()))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    return OUT


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股多策略实验 · 观测站</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0b1020; --panel:#141a2e; --panel2:#1b2340; --line:#27304f;
    --txt:#e6ebf5; --sub:#94a3b8; --up:#ef4444; --down:#22c55e; --accent:#6366f1;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:linear-gradient(180deg,#0b1020,#0d1326);color:var(--txt);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    padding:24px;max-width:1320px;margin:0 auto;-webkit-font-smoothing:antialiased}
  header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:6px}
  h1{font-size:22px;font-weight:700;letter-spacing:.5px}
  .ts{color:var(--sub);font-size:12px}
  .note{color:var(--sub);font-size:12.5px;margin:8px 0 20px;line-height:1.6}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px;margin-bottom:24px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 16px 14px;
    position:relative;overflow:hidden;transition:.2s}
  .card:hover{transform:translateY(-2px);border-color:#3a4568}
  .card .bar{position:absolute;left:0;top:0;bottom:0;width:4px}
  .card h3{font-size:14px;font-weight:600;margin-bottom:2px}
  .card .desc{font-size:11px;color:var(--sub);margin-bottom:12px;min-height:28px}
  .card .ret{font-size:26px;font-weight:800;letter-spacing:.5px}
  .card .total{font-size:13px;color:var(--sub);margin-top:4px}
  .card .pnlrow{display:flex;gap:12px;font-size:11px;margin-top:8px}
  .card .pnlrow b{font-weight:700}
  .card .meta{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);
    margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
  .pill{display:inline-block;font-size:10px;padding:1px 7px;border-radius:20px;margin-top:6px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:22px}
  .panel h2{font-size:15px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .chartwrap{position:relative;height:340px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--sub);font-weight:600;font-size:11px;letter-spacing:.5px}
  tr:hover td{background:rgba(255,255,255,.02)}
  .up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--sub)}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
  .tab{background:var(--panel2);border:1px solid var(--line);color:var(--sub);
    padding:6px 13px;border-radius:8px;font-size:12.5px;cursor:pointer;transition:.15s}
  .tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .empty{color:var(--sub);font-size:13px;padding:20px;text-align:center}
  .badge-side{font-size:10px;padding:1px 6px;border-radius:4px}
  .buy{background:rgba(239,68,68,.15);color:#f87171}
  .sell{background:rgba(34,197,94,.15);color:#4ade80}
  .sub2{font-size:13px;color:var(--sub);margin:16px 0 8px;font-weight:600}
  .tag{font-size:10px;color:var(--sub);border:1px solid var(--line);border-radius:4px;padding:0 5px;margin-left:6px}
  footer{color:var(--sub);font-size:11px;text-align:center;margin-top:30px;line-height:1.7}
  /* ---- 市场状态横幅 ---- */
  .regime{border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:6px 0 18px;
    display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
  .regime .rg-main{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .regime .rg-dot{width:9px;height:9px;border-radius:50%;flex:none}
  .regime .rg-icon{font-size:15px}
  .regime .rg-label{font-size:14px;font-weight:700}
  .regime .rg-gate{font-size:12px;color:var(--txt);opacity:.85}
  .regime .rg-sub{font-size:11px;color:var(--sub)}
  .regime .rg-badge{font-size:10px;padding:2px 9px;border-radius:20px;font-weight:600}
  .regime .rg-live{background:rgba(99,102,241,.18);color:#a5b4fc}
  .regime .rg-degraded{background:rgba(245,158,11,.18);color:#fbbf24}
  .regime .rg-metrics{display:flex;gap:16px;flex-wrap:wrap}
  .regime .rg-metric{font-size:11.5px;color:var(--sub)}
  .regime .rg-metric b{color:var(--txt);font-weight:700;margin-left:3px}
  .regime-normal{background:rgba(34,197,94,.08);border-color:rgba(34,197,94,.35)}
  .regime-normal .rg-dot{background:#22c55e;box-shadow:0 0 8px #22c55e}
  .regime-highvol{background:rgba(245,158,11,.09);border-color:rgba(245,158,11,.4)}
  .regime-highvol .rg-dot{background:#f59e0b;box-shadow:0 0 8px #f59e0b}
  .regime-riskoff{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.45)}
  .regime-riskoff .rg-dot{background:#ef4444;box-shadow:0 0 8px #ef4444}
  .regime-unknown,.regime-unknown .rg-dot{background:rgba(148,163,184,.12)}
  .regime-unknown .rg-dot{background:#94a3b8}
</style>
</head>
<body>
<header>
  <h1>📈 A股多策略实验 · 观测站</h1>
  <div class="ts">数据生成时间：__TS__ ｜ 行情：__LIVE__</div>
</header>
<div class="note">
  共 <b id="acct-count"></b> 个独立账户，各 100 万本金、完全隔离。持仓浮盈按<b>生成时实时现价</b>计算，
  已实现盈亏来自卖出记录。本页为静态快照——重新运行 <code>dashboard.py</code> 即可刷新。
</div>

__REGIME__

<div class="grid" id="cards"></div>

<div class="panel">
  <h2>📊 各组总资产走势</h2>
  <div class="chartwrap"><canvas id="equityChart"></canvas></div>
</div>

<div class="panel">
  <h2>💼 持仓个股收益 & 交易记录</h2>
  <div class="tabs" id="tabs"></div>
  <div id="detail"></div>
</div>

<footer>
  A股虚拟交易实验 · 数据来源于本地账本(state/equity/trades)+生成时实时行情 · 仅为模拟盘，不构成任何投资建议
</footer>

<script>
const DATA = __DATA__;
const fmt = n => Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmt0 = n => Number(n).toLocaleString('zh-CN',{maximumFractionDigits:0});
const cls = v => v>0?'up':(v<0?'down':'flat');
const sign = v => (v>0?'+':'')+Number(v).toFixed(2);

document.getElementById('acct-count').textContent = DATA.length;

// ---- 概览卡片 ----
const cards = document.getElementById('cards');
DATA.forEach((d)=>{
  const retCls = cls(d.ret);
  const status = d.exists ? (d.round>0?`已跑 ${d.round} 轮`:'待启动') : '未初始化';
  const wl = (d.win+d.loss)>0 ? `${d.win}胜${d.loss}负` : '无平仓';
  cards.innerHTML += `
  <div class="card">
    <div class="bar" style="background:${d.color}"></div>
    <h3>${d.name}</h3>
    <div class="desc">${d.desc}</div>
    <div class="ret ${retCls}">${sign(d.ret)}%</div>
    <div class="total">总资产 ¥${fmt(d.total)}</div>
    <div class="pnlrow">
      <span>浮盈 <b class="${cls(d.unrealized)}">${sign(d.unrealized)}</b></span>
      <span>已实现 <b class="${cls(d.realized)}">${sign(d.realized)}</b></span>
    </div>
    <div class="meta">
      <span>现金 ¥${(d.cash/10000).toFixed(1)}万 · 持仓 ${d.positions.length}只</span>
      <span>${wl}</span>
    </div>
    <span class="pill" style="background:rgba(255,255,255,.06);color:var(--sub)">${status}</span>
  </div>`;
});

// ---- 收益曲线 ----
const allTimes = [...new Set(DATA.flatMap(d=>d.equity.map(e=>e.t)))].sort();
const datasets = DATA.filter(d=>d.equity.length>0).map(d=>{
  const map = Object.fromEntries(d.equity.map(e=>[e.t,e.total]));
  let last=null;
  const pts = allTimes.map(t=>{ if(map[t]!=null) last=map[t]; return last; });
  return {label:d.name, data:pts, borderColor:d.color, backgroundColor:d.color+'22',
    borderWidth:2, tension:.25, pointRadius:2, pointHoverRadius:4, spanGaps:true};
});
new Chart(document.getElementById('equityChart'),{
  type:'line',
  data:{labels:allTimes.map(t=>t.slice(5,16)), datasets},
  options:{responsive:true,maintainAspectRatio:false,
    interaction:{mode:'index',intersect:false},
    plugins:{legend:{labels:{color:'#cbd5e1',boxWidth:14,font:{size:11}}},
      tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ¥${fmt(c.parsed.y)}`}}},
    scales:{
      x:{ticks:{color:'#64748b',maxTicksLimit:10,font:{size:10}},grid:{color:'#1e2740'}},
      y:{ticks:{color:'#64748b',font:{size:10},callback:v=>'¥'+(v/10000).toFixed(1)+'万'},
         grid:{color:'#1e2740'}}}}
});

// ---- 持仓/交易 Tab ----
const tabs = document.getElementById('tabs');
const detail = document.getElementById('detail');
DATA.forEach((d,i)=>{
  const t=document.createElement('div');
  t.className='tab'+(i===0?' active':''); t.textContent=d.name.split('·')[0];
  t.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active'); renderDetail(i);};
  tabs.appendChild(t);
});
function renderDetail(i){
  const d=DATA[i];
  let h=`<h3 style="font-size:13px;color:var(--sub);margin-bottom:6px">${d.name} — ${d.desc}</h3>`;
  h+=`<div style="font-size:12px;color:var(--sub);margin-bottom:12px">
        总资产 ¥${fmt(d.total)} ｜ 累计收益 <span class="${cls(d.ret)}">${sign(d.ret)}%</span>
        ｜ 持仓浮盈 <span class="${cls(d.unrealized)}">${sign(d.unrealized)}</span>
        ｜ 已实现盈亏 <span class="${cls(d.realized)}">${sign(d.realized)}</span>
        ｜ 平仓 ${d.win}胜${d.loss}负</div>`;

  // 个股收益（持仓）
  h+=`<div class="sub2">📌 持仓个股收益<span class="tag">现价为生成时实时价</span></div>`;
  if(d.positions.length){
    h+=`<table><thead><tr><th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>成本</th><th>现价</th><th>市值</th><th>浮动盈亏</th><th>浮盈%</th></tr></thead><tbody>`;
    d.positions.forEach(p=>{
      const lv = p.live?'':'<span class="tag">无实时价</span>';
      h+=`<tr><td>${p.code}</td><td>${p.name}${lv}</td><td>${p.qty}</td><td>${p.available}</td>
          <td>${p.cost}</td><td>${p.price}</td><td>¥${fmt0(p.mv)}</td>
          <td class="${cls(p.upnl)}">${sign(p.upnl)}</td>
          <td class="${cls(p.upnl_pct)}">${sign(p.upnl_pct)}%</td></tr>`;
    });
    h+=`</tbody></table>`;
  }else{h+=`<div class="empty">当前空仓</div>`;}

  // 交易记录
  h+=`<div class="sub2">📋 交易记录（最近30笔）</div>`;
  if(d.trades.length){
    h+=`<table><thead><tr><th>时间</th><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>成交额</th><th>实现盈亏</th><th>备注</th></tr></thead><tbody>`;
    d.trades.forEach(tr=>{const b=tr.side.includes('买')?'buy':'sell';
      const pnl = tr.pnl==null?'—':`<span class="${cls(tr.pnl)}">${sign(tr.pnl)}</span>`;
      h+=`<tr><td>${tr.t.slice(5)}</td><td><span class="badge-side ${b}">${tr.side}</span></td>
          <td>${tr.code}</td><td>${tr.name}</td><td>${tr.price}</td><td>${tr.qty}</td>
          <td>${fmt(tr.amount||0)}</td><td>${pnl}</td>
          <td style="text-align:left;color:var(--sub);font-size:11px;max-width:280px;white-space:normal">${tr.note}</td></tr>`;});
    h+=`</tbody></table>`;
  }else{h+=`<div class="empty">暂无成交记录</div>`;}
  detail.innerHTML=h;
}
renderDetail(0);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    use_live = "--no-live" not in sys.argv
    data, live_status = collect(use_live=use_live)
    path = render(data, live_status)
    print(f"观测站已生成: {path} | 行情状态: {live_status}")
    for d in data:
        print(f"  {d['name']}: 总资产 {d['total']:,.2f} | 收益 {d['ret']:+.3f}% | "
              f"浮盈 {d['unrealized']:+,.0f} | 已实现 {d['realized']:+,.0f} | "
              f"持仓 {len(d['positions'])}只 | 平仓 {d['win']}胜{d['loss']}负")
