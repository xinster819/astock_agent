/* ============================================================================
   对照实验观察台 · 交互层
   ----------------------------------------------------------------------------
   本层只负责【画】，不负责【判断】。所有会被人当作结论的数字——样本分级、
   可比性判据、可辨识性、回撤——都在 Python 侧算完随负载一起送来。
   这样每一个结论都有单元测试盯着，而不是藏在浏览器里。

   零外部依赖：图表手绘 SVG。这个文件要能在没有网络的机器上双击打开。
   ========================================================================= */
'use strict';

const DATA = JSON.parse(document.getElementById('payload').textContent);

/* ---- 状态 -------------------------------------------------------------- */
const state = {
  layer: 'all',
  mode: 'ret',
  sort: { key: 'ret', asc: false },
  picked: new Set(),
  hidden: new Set(),
};

/* ---- 工具 -------------------------------------------------------------- */
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

const num = (v, digits = 2) =>
  v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits);

const pct = (v, digits = 2) =>
  v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(digits)}%`;

const money = (v) =>
  v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });

/** 涨跌方向的语义类名。A 股惯例：红涨绿跌。 */
const dirClass = (v) => (v == null ? 'na' : v > 0 ? 'up' : v < 0 ? 'down' : '');

const parseTime = (s) => new Date(String(s).replace(' ', 'T')).getTime();

const live = () => DATA.accounts.filter((a) => a.exists);

/* ---- 判据条 ------------------------------------------------------------ */
function renderVerdict() {
  const v = DATA.verdict;
  const box = $('#verdict');
  box.classList.toggle('ok', v.ok);
  $('#verdict-icon').textContent = v.ok ? '✔' : '⚠';
  $('#verdict-headline').textContent = v.headline;

  const list = $('#verdict-reasons');
  list.replaceChildren(...v.reasons.map((r) => el('li', null, r)));

  const t = DATA.thresholds;
  $('#verdict-foot').textContent =
    `分级门槛：平仓 < ${t.min_trades_for_signal} 笔不作任何解读；` +
    `≥ ${t.min_trades_for_comparison} 笔才进入排名。` +
    `即便达标，${t.min_trades_for_comparison} 笔也远不足以做统计推断——` +
    `它只是「值得看一眼」的下限。`;
}

/* ---- 闸门条 ------------------------------------------------------------ */
function renderHealth() {
  const h = DATA.health;
  const box = $('#health');
  const chips = [];

  const chip = (warn, label, value, note) => {
    const c = el('div', `chip${warn ? ' warn' : ''}`);
    c.append(el('span', 'dot'));
    const b = el('b', null, label);
    c.append(b);
    if (value != null) c.append(el('span', 'n', value));
    if (note) c.append(el('span', null, note));
    return c;
  };

  chips.push(h.stalled.length
    ? chip(true, '引擎停摆', String(h.stalled.length), h.stalled.join(' '))
    : chip(false, '引擎', '13/13', '均进入过下单分支'));

  chips.push(h.dirty.length
    ? chip(true, '账本对账', String(h.dirty.length), `不符：${h.dirty.join(' ')}`)
    : chip(false, '账本对账', '13/13', '全部自洽'));

  if (h.never_closed.length) {
    chips.push(chip(true, '从未平仓', String(h.never_closed.length),
      `${h.never_closed.join(' ')} — 其收益全是浮盈，不构成业绩`));
  }

  const r = DATA.meta.regime;
  if (r) {
    const label = { normal: '常态', high_volatility: '高波动', risk_off: '避险' }[r.regime] || r.regime;
    chips.push(chip(r.degraded || r.regime === 'risk_off', '市场状态', label,
      r.degraded ? `降级值（${r.source}）` : ''));
  }

  box.replaceChildren(...chips);
}

/* ---- 排行榜 ------------------------------------------------------------ */
const SORTERS = {
  name: (a) => a.account,
  ret: (a) => (a.exists ? a.ret : -Infinity),
  dd: (a) => (a.curve && a.curve.max_drawdown_pct != null ? a.curve.max_drawdown_pct : -Infinity),
  win: (a) => (a.trade_stats && a.trade_stats.win_rate != null ? a.trade_stats.win_rate : -Infinity),
  closed: (a) => a.closed_trades || 0,
  round: (a) => a.round || 0,
  tier: (a) => ['insufficient', 'indicative', 'comparable'].indexOf(a.tier.key),
};

function visibleAccounts() {
  return DATA.accounts.filter(
    (a) => state.layer === 'all' || a.layer === state.layer
  );
}

/**
 * 排序。关键规则：**样本达标的账户永远排在不达标的前面**。
 *
 * 默认按收益降序时，一个 0 笔平仓、全是浮盈的账户会排到榜首——
 * 而榜首本身就是一种结论。再多的警告横幅也压不过「第一名」这个位置暗示的东西。
 * 所以分组是硬的：达标组在上，不达标组在下，中间用一行明说为什么。
 * 用户点表头改的是**组内**排序，改不了这个分组。
 */
function sortedAccounts() {
  const get = SORTERS[state.sort.key] || SORTERS.ret;
  const cmp = (x, y) => {
    const a = get(x);
    const b = get(y);
    const c = typeof a === 'string' ? a.localeCompare(b) : a - b;
    return state.sort.asc ? c : -c;
  };
  const rows = visibleAccounts();
  const rank = (a) => a.exists && a.tier.rank_eligible;
  return {
    eligible: rows.filter(rank).sort(cmp),
    rest: rows.filter((a) => !rank(a)).sort(cmp),
  };
}

function renderBoard() {
  const body = $('#board-body');
  const { eligible, rest } = sortedAccounts();
  const children = eligible.map(rowFor);
  if (rest.length) {
    if (eligible.length) children.push(dividerRow(rest.length));
    else children.push(dividerRow(rest.length, true));
    children.push(...rest.map(rowFor));
  }
  body.replaceChildren(...children);

  document.querySelectorAll('#board th[data-sort]').forEach((th) => {
    const on = th.dataset.sort === state.sort.key;
    th.classList.toggle('sorted', on);
    th.classList.toggle('asc', on && state.sort.asc);
  });

  const shown = visibleAccounts().filter((a) => a.exists).length;
  $('#board-foot').textContent =
    `显示 ${shown} 个已开张账户 · ${eligible.length} 个满足比较条件` +
    (state.picked.size ? ` · 已选中 ${state.picked.size} 个` : ' · 点行选中以对比曲线');
}

function rowFor(a) {
  const tr = el('tr');
  tr.dataset.account = a.account;
  if (state.picked.has(a.account)) tr.classList.add('picked');
  if (!a.exists || !a.tier.rank_eligible) tr.classList.add('dim');
  tr.style.color = a.color;

  const pick = el('td', 'c-pick');
  const sw = el('span', 'swatch');
  sw.style.background = a.color;
  pick.append(sw);
  tr.append(pick);

  const nameCell = el('td', 'c-name');
  const box = el('div', 'acct');
  box.append(el('span', 'id', a.account), el('span', 'nm', a.strategy));
  nameCell.append(box);
  tr.append(nameCell);

  if (!a.exists) {
    const td = el('td', 'na');
    td.colSpan = 6;
    td.textContent = '未初始化';
    tr.append(td);
    return tr;
  }

  tr.append(cell(pct(a.ret), `num ${dirClass(a.ret)}`));
  tr.append(cell(a.curve.max_drawdown_pct == null ? '—' : `${num(a.curve.max_drawdown_pct)}%`, 'num'));
  tr.append(cell(a.trade_stats.win_rate == null ? '—' : `${num(a.trade_stats.win_rate, 1)}%`,
    a.trade_stats.win_rate == null ? 'num na' : 'num'));
  tr.append(cell(String(a.closed_trades), 'num'));

  const tierCell = el('td', 'c-tier');
  tierCell.append(el('span', `tier tier-${a.tier.key}`, a.tier.label));
  tr.append(tierCell);

  tr.append(cell(String(a.round), 'num'));
  return tr;
}

const cell = (text, cls) => el('td', cls, text);

/** 分组分隔行：明说下面这些账户为什么不参与排名。 */
function dividerRow(count, noneAbove) {
  const tr = el('tr', 'divider');
  const td = el('td');
  td.colSpan = 8;
  td.textContent = noneAbove
    ? `以下 ${count} 个账户均未达到比较门槛，此处仅列出观测值，不构成排名`
    : `—— 以下 ${count} 个账户样本不足或未通过闸门，不参与排名 ——`;
  tr.append(td);
  return tr;
}

/* ---- 曲线 -------------------------------------------------------------- */
const PAD = { top: 12, right: 14, bottom: 26, left: 46 };

/** 把一个账户的权益曲线换算成当前口径下的 [{x, y}]。 */
function seriesFor(account) {
  const pts = account.equity || [];
  if (!pts.length) return [];

  if (state.mode === 'ret') {
    return pts.map((p) => ({ x: parseTime(p.t), y: p.ret }));
  }
  if (state.mode === 'norm') {
    const base = pts[0].total;
    if (!base) return [];
    return pts.map((p) => ({ x: parseTime(p.t), y: (p.total / base - 1) * 100 }));
  }
  // 回撤：距历史峰值的跌幅，恒 ≤ 0
  let peak = -Infinity;
  return pts.map((p) => {
    peak = Math.max(peak, p.total);
    return { x: parseTime(p.t), y: peak > 0 ? -((peak - p.total) / peak) * 100 : 0 };
  });
}

/** 基准指数换算成同口径的百分比曲线。回撤模式下不画基准。 */
function benchSeries() {
  const b = DATA.benchmark;
  if (!b || !b.points || !b.points.length || state.mode === 'dd') return null;
  const base = b.points[0].close;
  if (!base) return null;
  return {
    name: b.name,
    points: b.points.map((p) => ({ x: parseTime(p.t), y: (p.close / base - 1) * 100 })),
  };
}

function chartables() {
  return visibleAccounts()
    .filter((a) => a.exists && !state.hidden.has(a.account))
    .map((a) => ({ account: a, points: seriesFor(a) }))
    .filter((s) => s.points.length > 1);
}

function renderChart() {
  const svg = $('#curve');
  const rect = svg.getBoundingClientRect();
  const W = Math.max(rect.width || 640, 320);
  const H = Math.max(rect.height || 340, 220);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const series = chartables();
  const bench = benchSeries();
  svg.replaceChildren();

  if (!series.length) {
    svg.append(svgText(W / 2, H / 2, '没有可绘制的曲线', 'middle'));
    renderLegend([]);
    return;
  }

  const all = series.flatMap((s) => s.points).concat(bench ? bench.points : []);
  const xs = all.map((p) => p.x);
  const ys = all.map((p) => p.y);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  let y0 = Math.min(...ys);
  let y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const padY = (y1 - y0) * 0.08;
  y0 -= padY; y1 += padY;

  const sx = (x) => PAD.left + ((x - x0) / (x1 - x0 || 1)) * (W - PAD.left - PAD.right);
  const sy = (y) => PAD.top + (1 - (y - y0) / (y1 - y0 || 1)) * (H - PAD.top - PAD.bottom);

  drawAxes(svg, { W, H, x0, x1, y0, y1, sx, sy });

  if (bench) {
    svg.append(path(bench.points, sx, sy, 'series bench', 'var(--faint)'));
  }
  const dimOthers = state.picked.size > 0;
  for (const s of series) {
    const on = !dimOthers || state.picked.has(s.account.account);
    svg.append(path(s.points, sx, sy, `series${on ? '' : ' muted'}`, s.account.color));
  }

  renderLegend(series, bench);
  wireHover(svg, { W, H, series, bench, sx, sy, x0, x1 });
}

function drawAxes(svg, { W, H, y0, y1, x0, x1, sx, sy }) {
  const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  g.setAttribute('class', 'axis');

  const ticks = niceTicks(y0, y1, 5);
  for (const t of ticks) {
    const y = sy(t);
    const line = svgLine(PAD.left, y, W - PAD.right, y);
    if (Math.abs(t) < 1e-9) line.setAttribute('class', 'zero');
    g.append(line, svgText(PAD.left - 7, y + 3, `${t.toFixed(1)}%`, 'end'));
  }

  const span = x1 - x0;
  for (let i = 0; i <= 4; i++) {
    const x = x0 + (span * i) / 4;
    const d = new Date(x);
    const label = `${d.getMonth() + 1}/${String(d.getDate()).padStart(2, '0')}`;
    g.append(svgText(sx(x), H - PAD.bottom + 15, label, i === 0 ? 'start' : i === 4 ? 'end' : 'middle'));
  }
  svg.append(g);
}

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) out.push(t);
  return out;
}

function path(points, sx, sy, cls, stroke) {
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`).join('');
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  node.setAttribute('d', d);
  node.setAttribute('class', cls);
  node.setAttribute('stroke', stroke);
  return node;
}

function svgLine(x1, y1, x2, y2) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  n.setAttribute('x1', x1); n.setAttribute('y1', y1);
  n.setAttribute('x2', x2); n.setAttribute('y2', y2);
  return n;
}

function svgText(x, y, text, anchor) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  n.setAttribute('x', x); n.setAttribute('y', y);
  n.setAttribute('text-anchor', anchor || 'start');
  n.textContent = text;
  return n;
}

function renderLegend(series, bench) {
  const box = $('#legend');
  const items = series.map((s) => {
    const item = el('div', `item${state.hidden.has(s.account.account) ? ' off' : ''}`);
    const bar = el('span', 'bar');
    bar.style.background = s.account.color;
    item.append(bar, el('span', null, s.account.account));
    item.onclick = () => {
      const id = s.account.account;
      state.hidden.has(id) ? state.hidden.delete(id) : state.hidden.add(id);
      renderChart();
    };
    return item;
  });
  if (bench) {
    const item = el('div', 'item');
    const bar = el('span', 'bar');
    bar.style.background = 'var(--faint)';
    item.append(bar, el('span', null, `${bench.name}（基准）`));
    items.push(item);
  } else if (DATA.benchmark && DATA.benchmark.error) {
    items.push(el('div', 'item', '基准未取到（离线或取数失败）'));
  }
  box.replaceChildren(...items);
}

/* ---- 悬停读数 ---------------------------------------------------------- */
function wireHover(svg, ctx) {
  const tip = $('#tooltip');
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  svg.append(marker);

  svg.onmouseleave = () => { tip.hidden = true; marker.replaceChildren(); };
  svg.onmousemove = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * ctx.W;
    if (px < PAD.left || px > ctx.W - PAD.right) { tip.hidden = true; marker.replaceChildren(); return; }

    const t = ctx.x0 + ((px - PAD.left) / (ctx.W - PAD.left - PAD.right)) * (ctx.x1 - ctx.x0);
    const rows = [];
    marker.replaceChildren(svgLine(px, PAD.top, px, ctx.H - PAD.bottom));
    marker.firstChild.setAttribute('class', 'hoverline');

    const shown = ctx.series.filter(
      (s) => state.picked.size === 0 || state.picked.has(s.account.account)
    );
    for (const s of shown) {
      const p = nearest(s.points, t);
      if (!p) continue;
      rows.push({ label: s.account.account, value: p.y, color: s.account.color });
      const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      dot.setAttribute('cx', ctx.sx(p.x)); dot.setAttribute('cy', ctx.sy(p.y));
      dot.setAttribute('r', 3); dot.setAttribute('fill', s.account.color);
      dot.setAttribute('class', 'hoverdot');
      marker.append(dot);
    }
    if (ctx.bench) {
      const p = nearest(ctx.bench.points, t);
      if (p) rows.push({ label: ctx.bench.name, value: p.y, color: 'var(--faint)' });
    }
    if (!rows.length) { tip.hidden = true; return; }

    rows.sort((a, b) => b.value - a.value);
    tip.replaceChildren(
      el('div', 'tt-t', new Date(t).toLocaleString('zh-CN', { hour12: false })),
      ...rows.slice(0, 8).map((r) => {
        const row = el('div', 'tt-row');
        const name = el('span', null, r.label);
        name.style.color = r.color;
        const val = el('span', dirClass(r.value), pct(r.value));
        row.append(name, val);
        return row;
      })
    );
    tip.hidden = false;
    const wrapper = svg.parentElement.getBoundingClientRect();
    const left = event.clientX - wrapper.left + 14;
    tip.style.left = `${Math.min(left, wrapper.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${event.clientY - wrapper.top + 12}px`;
  };
}

function nearest(points, t) {
  let best = null;
  let bestDist = Infinity;
  for (const p of points) {
    const d = Math.abs(p.x - t);
    if (d < bestDist) { bestDist = d; best = p; }
  }
  return best;
}

/* ---- 归因面板 ---------------------------------------------------------- */
function renderDetail() {
  const body = $('#detail-body');
  const id = [...state.picked].pop();
  const a = id && DATA.accounts.find((x) => x.account === id);

  if (!a || !a.exists) {
    $('#detail-title').textContent = '归因';
    $('#detail-hint').textContent = id ? `${id} 尚未初始化` : '点左侧任意账户查看';
    body.replaceChildren(el('p', 'empty', '选中一个账户后，这里显示它的绩效构成、持仓与成交流水。'));
    return;
  }

  $('#detail-title').textContent = `${a.account} · ${a.strategy}`;
  $('#detail-hint').textContent = a.desc || '';

  const ts = a.trade_stats;
  // 「可辨识」本身不含褒贬——exp5 的可辨识是**稳定在亏**。
  // 只写「可辨识」会被扫读的人当成好消息，所以必须带方向。
  const edge = ts.edge_is_detectable;
  const positive = (ts.expectancy || 0) > 0;
  const edgeText = edge === true
    ? (positive ? '优势可辨识' : '劣势可辨识')
    : edge === false ? '与随机不可区分' : '样本不足，不判';
  const edgeNote = edge === true
    ? `单笔期望 ${money(ts.expectancy)} ± ${money(ts.std_error)}（标准误差）`
    : edge === false
      ? '盈亏均值落在标准误差区间内，尚看不出方向'
      : `平仓仅 ${ts.closed} 笔，低于 ${DATA.thresholds.min_trades_for_signal} 笔不作判定`;

  const metrics = el('div', 'metrics');
  metrics.append(
    metric('累计收益', pct(a.ret), dirClass(a.ret), `总资产 ${money(a.total)}`),
    metric('最大回撤', a.curve.max_drawdown_pct == null ? '—' : `${num(a.curve.max_drawdown_pct)}%`,
      '', `${a.curve.observations} 个观测点 · ${a.curve.span_days ?? '—'} 天`),
    metric('已实现盈亏', money(a.realized), dirClass(a.realized),
      `浮动 ${money(a.unrealized)}`),
    metric('胜率', ts.win_rate == null ? '—' : `${num(ts.win_rate, 1)}%`, '',
      `${ts.wins} 胜 / ${ts.losses} 负`),
    metric('盈亏比', ts.profit_factor == null ? '—' : num(ts.profit_factor), '',
      ts.profit_factor == null ? '尚无亏损笔' : '总盈利 / 总亏损'),
    metric('统计判定', edgeText, edge === true ? dirClass(ts.expectancy) : 'na', edgeNote),
    metric('仓位集中度', a.concentration == null ? '—' : `${num(a.concentration, 1)}%`,
      '', '最大单票占总资产'),
  );

  const grid = el('div', 'subgrid');
  grid.append(positionsBlock(a), tradesBlock(a));

  const parts = [metrics];
  if (a.red_flags && a.red_flags.length) {
    const flags = el('div', 'flags');
    flags.append(el('h3', null, '账本红旗'));
    for (const f of a.red_flags) flags.append(el('div', 'flag', `[${f.check}] ${f.detail}`));
    parts.push(flags);
  }
  parts.push(grid);
  body.replaceChildren(...parts);
}

function metric(key, value, cls, note) {
  const box = el('div', 'metric');
  box.append(el('div', 'k', key));
  box.append(el('div', `v ${cls || ''}`, value));
  if (note) box.append(el('div', 'note', note));
  return box;
}

function positionsBlock(a) {
  const box = el('div');
  box.append(el('h3', null, `当前持仓（${a.positions.length}）`));
  if (!a.positions.length) {
    box.append(el('p', 'empty', '空仓。'));
    return box;
  }
  const table = el('table');
  table.append(head(['代码', '名称', '数量', '成本', '现价', '浮盈']));
  const tbody = el('tbody');
  for (const p of a.positions) {
    const tr = el('tr');
    tr.append(cell(p.code, 'mono'), cell(p.name),
      cell(String(p.qty), 'num'), cell(num(p.cost, 3), 'num'),
      cell(num(p.price, 3), 'num'),
      cell(pct(p.upnl_pct), `num ${dirClass(p.upnl_pct)}`));
    tbody.append(tr);
  }
  table.append(tbody);
  box.append(table);
  return box;
}

function tradesBlock(a) {
  const box = el('div');
  box.append(el('h3', null, `最近成交（${a.trades.length}）`));
  if (!a.trades.length) {
    box.append(el('p', 'empty', '尚无成交。'));
    return box;
  }
  const table = el('table');
  table.append(head(['时间', '方向', '标的', '数量', '盈亏']));
  const tbody = el('tbody');
  for (const t of a.trades.slice(0, 14)) {
    const tr = el('tr');
    tr.append(
      cell(String(t.t).slice(5, 16), 'mono'),
      cell(t.side, t.side.includes('买') ? 'up' : 'down'),
      cell(`${t.name || t.code}`),
      cell(String(t.qty), 'num'),
      cell(t.pnl == null ? '—' : money(t.pnl), `num ${dirClass(t.pnl)}`)
    );
    tbody.append(tr);
  }
  table.append(tbody);
  box.append(table);
  return box;
}

function head(labels) {
  const thead = el('thead');
  const tr = el('tr');
  labels.forEach((l, i) => {
    const th = el('th', i >= 2 ? 'num' : null, l);
    th.style.cursor = 'default';
    tr.append(th);
  });
  thead.append(tr);
  return thead;
}

/* ---- 持仓重叠 ---------------------------------------------------------- */
function renderOverlap() {
  const box = $('#overlap-body');
  const rows = DATA.overlap.filter((h) => h.held_by.length > 0);
  if (!rows.length) {
    box.replaceChildren(el('p', 'empty', '当前无人持仓。'));
    return;
  }
  box.replaceChildren(...rows.slice(0, 12).map((h) => {
    const row = el('div', 'ovrow');
    const code = el('div', 'code');
    code.append(el('b', null, h.code), el('span', null, h.name || ''));
    const holders = el('div', 'holders');
    for (const id of h.held_by) {
      holders.append(el('span', `holder${h.held_by.length >= 3 ? ' hot' : ''}`, id));
    }
    row.append(code, holders, el('div', 'num', money(h.total_mv)));
    return row;
  }));
}

/* ---- 事件接线 ---------------------------------------------------------- */
function wire() {
  $('#board-body').addEventListener('click', (event) => {
    const tr = event.target.closest('tr[data-account]');
    if (!tr) return;
    const id = tr.dataset.account;
    state.picked.has(id) ? state.picked.delete(id) : state.picked.add(id);
    renderBoard();
    renderChart();
    renderDetail();
  });

  document.querySelectorAll('#board th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) state.sort.asc = !state.sort.asc;
      else state.sort = { key, asc: key === 'name' };
      renderBoard();
    });
  });

  document.querySelectorAll('[data-layer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.layer = btn.dataset.layer;
      document.querySelectorAll('[data-layer]').forEach((b) => b.classList.toggle('on', b === btn));
      renderBoard();
      renderChart();
    });
  });

  document.querySelectorAll('[data-mode]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.mode = btn.dataset.mode;
      document.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('on', b === btn));
      renderChart();
    });
  });

  $('#theme').addEventListener('click', () => {
    const root = document.documentElement;
    const now = root.getAttribute('data-theme');
    const next = now === 'dark' ? 'light' : now === 'light' ? null : 'dark';
    next ? root.setAttribute('data-theme', next) : root.removeAttribute('data-theme');
    try { next ? localStorage.setItem('astock-theme', next) : localStorage.removeItem('astock-theme'); } catch (_) { /* 隐私模式 */ }
    renderChart();
  });

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderChart, 120);
  });
}

/* ---- 启动 -------------------------------------------------------------- */
function boot() {
  try {
    const saved = localStorage.getItem('astock-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (_) { /* 隐私模式下 localStorage 不可用，忽略 */ }

  $('#generated').textContent = `生成于 ${DATA.meta.generated_at}`;
  $('#livestatus').textContent = DATA.meta.live_status;
  $('#workspace').textContent = DATA.meta.workspace;

  renderVerdict();
  renderHealth();
  renderBoard();
  renderChart();
  renderDetail();
  renderOverlap();
  wire();
}

boot();
