import Chart from "chart.js/auto";
import "./abrain.css";
import { initializeHub } from "./hub.js";
Chart.defaults.color = "#aeb7c7";
Chart.defaults.borderColor = "rgba(255,255,255,.09)";
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "—").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const text = (id, v) => {
  $(id).textContent = v ?? "—";
};
const money = (v, c) =>
  v == null
    ? "未返回"
    : `${c} ${Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const num = (v) => (v == null ? "—" : Number(v).toLocaleString("zh-CN"));
const pct = (v) =>
  v == null ? "无可比基期" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
const change = (a, b) =>
  a != null && b != null && Number(b) !== 0
    ? ((Number(a) - Number(b)) / Math.abs(Number(b))) * 100
    : null;
const date = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const add = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
function parse(v) {
  const m = v.trim().match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/);
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return d.getFullYear() === +m[1] &&
    d.getMonth() === +m[2] - 1 &&
    d.getDate() === +m[3]
    ? d
    : null;
}
const today = new Date();
today.setHours(0, 0, 0, 0);
const yesterday = add(today, -1);
const state = {
  site: "MY",
  store: "ALL",
  start: add(yesterday, -6),
  end: yesterday,
  draftStart: add(yesterday, -6),
  draftEnd: yesterday,
  left: new Date(today.getFullYear(), today.getMonth(), 1),
  first: true,
  mode: "detail",
  expense: null,
  sku: "all",
  sort: "orders",
};
let shops = [],
  report = null,
  generation = 0,
  controller = null,
  job = null,
  timer = null,
  chatBusy = false,
  detail = null,
  detailVersion = 0;
const charts = {};
const automaticRequests = new Set();
let displayedProgress = -1;
const names = {
  orders: "订单与省州",
  finance: "结算与费用",
  affiliate: "联盟与达人",
  performance: "渠道归因GMV",
  videos: "商品关联视频",
};
const statuses = {
  complete: "完整读取",
  partial: "部分数据",
  missing: "未同步",
};
const params = () => ({
  site: state.site,
  shop_id: state.store,
  start_date: date(state.start),
  end_date: date(state.end),
});
const query = () => new URLSearchParams(params()).toString();
const current = () => (report?.shops.length === 1 ? report.shops[0] : null);
async function api(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Shop-ID": localStorage.getItem("shop") || "default",
      ...(sessionStorage.getItem("token")
        ? { Authorization: "Bearer " + sessionStorage.getItem("token") }
        : {}),
      ...options.headers,
    },
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw Error(
      r.status === 401
        ? "请返回运营助手，在连接与设置中填写工作台访问令牌。"
        : typeof d.detail === "string"
          ? d.detail
          : "请求失败，请检查筛选和连接。",
    );
  }
  return r;
}
const json = async (path, options) => (await api(path, options)).json();
function status(message, error = false) {
  $("dataStatus").classList.toggle("error", error);
  text("dataStatus", message);
}
function chart(id, type, rows, label, opts = {}) {
  charts[id]?.destroy();
  const canvas = $(id),
    box = canvas.parentElement;
  box.querySelector(".chart-empty")?.remove();
  canvas.hidden = !rows.length;
  if (!rows.length) {
    const note = document.createElement("div");
    note.className = "chart-empty";
    note.textContent = "当前筛选没有可用数据";
    box.append(note);
    return;
  }
  charts[id] = new Chart(canvas, {
    type,
    data: {
      labels: rows.map((r) => r.label),
      datasets: [
        {
          label,
          data: rows.map((r) => (r.value == null ? null : Number(r.value))),
          backgroundColor:
            type === "doughnut"
              ? [
                  "#2dd4bf",
                  "#2563eb",
                  "#f59e0b",
                  "#e11d48",
                  "#64748b",
                  "#94a3b8",
                ]
              : "#2dd4bf",
          borderColor: "#2dd4bf",
          borderWidth: type === "line" ? 2 : 0,
          borderRadius: 5,
          tension: 0.25,
          pointRadius: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: type === "doughnut", position: "bottom" } },
      ...(type === "doughnut"
        ? {}
        : {
            scales: {
              [opts.indexAxis === "y" ? "x" : "y"]: { beginAtZero: true },
            },
          }),
      ...opts,
    },
  });
}
const rowsHtml = (rows, fn) =>
  rows.length
    ? rows.map(fn).join("")
    : '<tr><td colspan="8" class="empty-data">当前筛选没有可用记录</td></tr>';
function view(mode) {
  state.mode = mode;
  document
    .querySelectorAll("#viewModeGroup button")
    .forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("detailView").classList.toggle("hidden", mode !== "detail");
  $("compareView").classList.toggle("hidden", mode !== "compare");
}
function filters() {
  const sites = [...new Set(shops.map((s) => s.region))];
  $("siteSelect").innerHTML =
    sites
      .map((s) => `<option value="${esc(s)}">站点：${esc(s)}</option>`)
      .join("") + '<option value="ALL">全部站点 · 按原币对比</option>';
  $("siteSelect").value = state.site;
  const selected = shops.filter(
    (s) => state.site === "ALL" || s.region === state.site,
  );
  if (!selected.some((s) => s.id === state.store)) state.store = "ALL";
  $("storeSelect").innerHTML =
    '<option value="ALL">全部已授权店铺</option>' +
    selected
      .map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`)
      .join("");
  $("storeSelect").value = state.store;
}
function coverage() {
  let cards = "";
  for (const r of report?.shops || [])
    for (const period of ["current", "previous"])
      for (const [s, c] of Object.entries(r[period].coverage)) {
        cards += `<div class="coverage-item"><strong>${esc(r.shop.name)} · ${period === "current" ? "本期" : "对比期"}</strong><br>${names[s]}：${statuses[c.status]}${c.stale ? " · 可刷新" : ""}<br>${c.retrieved_at ? new Date(c.retrieved_at * 1000).toLocaleString("zh-CN") : "未获取"}<br>来源请求 ${c.evidence.length} 次${c.last_attempt?.status === "failed" ? `<br><strong>最近刷新失败${c.last_attempt.platform_code ? "（平台码 " + c.last_attempt.platform_code + "）" : ""}，当前保留上次成功快照</strong>` : ""}</div>`;
      }
  $("coverageContent").innerHTML =
    '<p class="source-note">订单按店铺当地创建日期；结算按UTC账单生成日期；渠道按平台支付期。未知不补0，金额保留原币。</p><div class="coverage-grid">' +
    cards +
    "</div>";
}
function render() {
  if (document.activeElement !== $("startDateInput"))
    $("startDateInput").value = date(state.start).replaceAll("-", "/");
  if (document.activeElement !== $("endDateInput"))
    $("endDateInput").value = date(state.end).replaceAll("-", "/");
  text(
    "compareRangeText",
    report
      ? `${report.previous_window.start_date} — ${report.previous_window.end_date}`
      : "前一等长周期",
  );
  text(
    "activeWindowSubtitle",
    `${date(state.start)} — ${date(state.end)} · ${current()?.shop.timezone || "各店铺当地时间"} · 对比前一等长周期`,
  );
  coverage();
  sourceSummary();
  comparison();
  const r = current();
  document.querySelector('#viewModeGroup [data-mode="detail"]').disabled = !r;
  if (!r) {
    view("compare");
    status(
      report?.shops.length
        ? "已按店铺原币展示横向对比，跨币种金额不相加。点击店铺行查看明细。"
        : "没有符合筛选条件的已授权店铺。",
    );
    return;
  }
  view(state.mode);
  const p = r.current,
    b = r.previous,
    c = r.shop.currency,
    o = p.orders,
    f = p.finance,
    a = p.affiliate;
  text("kpiTotalOrders", num(o?.total));
  $("kpiTotalOrders").classList.add("metric-button");
  $("kpiTotalOrders").onclick = () => openDetails("orders");
  text("kpiDeliveredOrders", num(o?.delivered));
  text(
    "kpiDeliveredRate",
    o?.total ? `${((o.delivered / o.total) * 100).toFixed(1)}%` : "—",
  );
  text("kpiInTransit", `在途/待发 ${num(o?.in_transit)} 单`);
  text("kpiUnsignedOrders", num(o?.cancelled));
  text(
    "kpiUnsignedRate",
    o?.total ? `${((o.cancelled / o.total) * 100).toFixed(1)}%` : "—",
  );
  text(
    "kpiGmvAov",
    `平台GMV ${money(p.performance?.gmv, c)} · AOV ${money(p.performance?.avg_order_value, c)}`,
  );
  const comm =
    a?.actual_std != null || a?.actual_ads != null
      ? Number(a?.actual_std || 0) + Number(a?.actual_ads || 0)
      : null;
  text("kpiCreatorCommTotal", money(comm, c));
  text(
    "kpiCreatorCommSplit",
    `已知标准 ${money(a?.actual_std, c)} / 广告 ${money(a?.actual_ads, c)}`,
  );
  for (const [prefix, k] of [
    ["normal", "positive"],
    ["unpaid", "zero"],
    ["neg", "negative"],
  ]) {
    text(prefix + "PayoutCount", num(f?.[k]?.orders));
    text(prefix + "PayoutAmt", money(f?.[k]?.amount, c));
    text(
      prefix + "PayoutPct",
      f?.order_count
        ? `占本期结算订单 ${((f[k].orders / f.order_count) * 100).toFixed(1)}%`
        : "无可用结算订单",
    );
  }
  text("normalPayoutRateBadge", `结算：${statuses[p.coverage.finance.status]}`);
  text(
    "settledBadge",
    `${num(f?.statement_count)} 份账单 · ${num(f?.order_count)} 个订单`,
  );
  text("netSettlementVal", money(f?.net_settlement, c));
  text(
    "netSettlementCompare",
    `上期 ${money(b.finance?.net_settlement, c)} · ${pct(change(f?.net_settlement, b.finance?.net_settlement))}`,
  );
  text("revAndFeeVal", `${money(f?.revenue, c)} / ${money(f?.fees, c)}`);
  text(
    "revAndFeeCompare",
    f?.statement_transaction_difference == null
      ? "账单与流水暂不可核对；金额不等于利润"
      : `账单减流水差额 ${money(f.statement_transaction_difference, c)} · ${Number(f.statement_transaction_difference) === 0 ? "已对平" : "待核对"}`,
  );
  text("abnormalSettleVal", money(f?.paid_statement_amount, c));
  text("unpaidPlusNegTotal", `调整流水 ${num(f?.adjustments?.length)} 笔`);
  $("payoutReasonList").innerHTML =
    (f?.statement_statuses || [])
      .map(
        (v) =>
          `<div class="reason-row"><strong>${esc(v.label)}</strong><span class="mono"> ${v.value} 份账单</span></div>`,
      )
      .join("") +
    `<p class="source-note">${esc(f?.note || "尚未同步结算流水。")}</p><button class="ai-chip-btn" id="settlementDetails">查看结算流水明细</button>`;
  $("settlementDetails").onclick = () => openDetails("settlement");
  const expenses = f?.expenses || [];
  text(
    "totalExpenseSumLabel",
    `非零费用字段 ${expenses.length} 项 · 正数可能为退费`,
  );
  if (!expenses.some((e) => e.id === state.expense))
    state.expense = expenses[0]?.id;
  $("expenseList").innerHTML = expenses.length
    ? expenses
        .map((e) => {
          const prev = b.finance?.expenses.find((v) => v.id === e.id);
          return `<button type="button" class="expense-item ${e.id === state.expense ? "active" : ""}" data-expense="${esc(e.id)}" style="width:100%;text-align:left"><div class="expense-top"><span><span class="expense-name">${esc(e.name)}</span><span class="expense-field mono">${esc(e.id)}</span></span><span class="mono">${money(e.amount, c)}<br><small>${pct(change(e.amount, prev?.amount))}</small></span></div><div class="expense-meta-mini"><span>非零 ${e.has_orders} 单 / 明确零 ${e.no_orders} 单 / 未知 ${e.unknown_orders} 单</span></div></button>`;
        })
        .join("")
    : '<p class="empty-data">暂无非零费用明细；未同步不代表费用为零。</p>';
  $("expenseList")
    .querySelectorAll("[data-expense]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          state.expense = b.dataset.expense;
          render();
        }),
    );
  expense(expenses.find((e) => e.id === state.expense));
  skus();
  creators();
  chart("regionChart", "bar", o?.regions || [], "订单数", { indexAxis: "y" });
  text("regionTotalBadge", `${num(o?.total)} 单 · 仅省州`);
  chart("orderStatusChart", "doughnut", o?.statuses || [], "订单数");
  chart(
    "unsignedReasonChart",
    "bar",
    o?.cancellation_reasons || [],
    "取消订单数",
  );
  text("unsignedBadge", `已取消 ${num(o?.cancelled)} 单`);
  chart(
    "hourChart",
    "line",
    o ? o.hours.map((v, i) => ({ label: `${i}:00`, value: v })) : [],
    "下单数量",
  );
  const peak = o ? Math.max(...o.hours) : 0;
  text(
    "peakHourBadge",
    !o
      ? "下单时段尚未读取"
      : peak
        ? `峰值 ${o.hours.indexOf(peak)}:00 · ${peak} 单`
        : "当前没有下单记录",
  );
  chart(
    "channelChart",
    "bar",
    p.performance?.channels || [],
    `归因GMV（${c}）`,
  );
  text(
    "channelTopBadge",
    `平台截至 ${p.performance?.latest_available_date || "未返回"}`,
  );
  insights();
  if (!job || !["queued", "running"].includes(job.status))
    status(
      `已读取 ${r.shop.name} 的真实快照。${Object.values(p.coverage).some((v) => v.status !== "complete") ? "部分来源未同步或不完整，请查看来源状态。" : "各来源已完成分页读取。"}结算与订单采用不同日期口径。`,
    );
}
function expense(e) {
  const r = current(),
    c = r.shop.currency,
    prev = r.previous.finance?.expenses.find((v) => v.id === e?.id);
  text("inspTitle", e?.name || "请选择费用字段");
  text("inspSub", e ? `${e.id} · ${money(e.amount, c)}` : "没有可用非零费用");
  text("inspMomBadge", pct(change(e?.amount, prev?.amount)));
  text("inspHasCount", e ? `${e.has_orders} 单` : "—");
  text("inspHasRate", "点击数量查看订单");
  text("inspNoCount", e ? `${e.no_orders} 单` : "—");
  text(
    "inspNoNote",
    e ? `字段未知 ${e.unknown_orders} 单，未计入明确零` : "字段未返回",
  );
  text(
    "inspShareRatio",
    e?.share_pct == null ? "—" : `${e.share_pct.toFixed(1)}%`,
  );
  text(
    "inspShareFormula",
    e ? `绝对费用 ÷ 对应订单营收 ${money(e.base_revenue, c)}` : "无可计算营收",
  );
  for (const [id, zero] of [
    ["inspHasCount", false],
    ["inspNoCount", true],
  ]) {
    $(id).classList.toggle("metric-button", !!e);
    $(id).onclick = e ? () => openDetails("expense", e.id, zero) : null;
    $(id).setAttribute("role", "button");
    $(id).tabIndex = e ? 0 : -1;
    $(id).onkeydown = (event) => {
      if (e && ["Enter", " "].includes(event.key)) {
        event.preventDefault();
        openDetails("expense", e.id, zero);
      }
    };
  }
  chart(
    "expenseChart",
    "bar",
    e
      ? [
          { label: "非零费用", value: e.has_orders },
          { label: "明确零费用", value: e.no_orders },
          { label: "字段未知", value: e.unknown_orders },
        ]
      : [],
    "订单数",
  );
}
function skus() {
  const r = current();
  if (!r) return;
  const rows = r.skus.filter((v) =>
    state.sku === "up"
      ? v.unit_delta > 0
      : state.sku === "down"
        ? v.unit_delta < 0
        : true,
  );
  $("skuTableBody").innerHTML = rowsHtml(
    rows,
    (v) =>
      `<tr class="clickable-row" data-sku="${esc(v.id)}"><td><strong>${esc(v.name)}</strong><div class="mono">${esc(v.sku)}</div></td><td>${num(v.current_units)} 件</td><td>${num(v.previous_units)} 件</td><td><span class="pill-badge ${v.unit_delta > 0 ? "pill-pos" : v.unit_delta < 0 ? "pill-neg" : ""}">${pct(v.change_pct)}</span></td><td>${v.product_video_count == null ? "未获取视频" : `${v.product_video_count} 条商品关联视频`}<br><small>商品级，非SKU归因</small></td><td>尚无SKU广告归因</td><td>仅展示证据，不推断涨跌原因</td></tr>`,
  );
  $("skuTableBody")
    .querySelectorAll("[data-sku]")
    .forEach(
      (tr) =>
        (tr.onclick = () => {
          const v = rows.find((s) => s.id === tr.dataset.sku);
          text(
            "skuDiagBox",
            `${v.sku}：本期 ${num(v.current_units)} 件，上期 ${num(v.previous_units)} 件。${pct(v.change_pct)}。已支付非取消订单件数，未扣售后退货；无充分证据判断视频或广告导致变化。`,
          );
          openDetails("sku", v.id);
        }),
    );
  if (!rows.length)
    text("skuDiagBox", "当前筛选没有SKU记录，或订单来源尚未同步。");
}
function creators() {
  const r = current();
  if (!r) return;
  const a = r.current.affiliate,
    c = r.shop.currency;
  const cards = [
    [
      "联盟订单 / 未分类",
      a ? `${a.affiliate_orders} / ${num(a.unclassified_orders)}` : "—",
      "未匹配不等于非达人或自营",
    ],
    [
      "已知实际标准佣金",
      money(a?.actual_std, c),
      "仅SETTLED且明确返回金额的行",
    ],
    [
      "已知实际广告佣金",
      money(a?.actual_ads, c),
      "空字段不补0；预估与实际分开",
    ],
  ];
  $("creatorSplitCards").innerHTML = cards
    .map(
      (v) =>
        `<div class="recon-stat-card"><div class="recon-stat-label">${v[0]}</div><div class="recon-stat-main mono">${v[1]}</div><div class="recon-stat-sub">${v[2]}</div></div>`,
    )
    .join("");
  const key = { orders: "orders", stdComm: "actual_std", adComm: "actual_ads" }[
    state.sort
  ];
  const rows = [...(a?.creators || [])].sort(
    (x, y) => Number(y[key] ?? -Infinity) - Number(x[key] ?? -Infinity),
  );
  $("creatorTableBody").innerHTML = rowsHtml(rows, (v, i) => {
    const total =
      v.actual_std == null && v.actual_ads == null
        ? null
        : Number(v.actual_std || 0) + Number(v.actual_ads || 0);
    return `<tr><td>#${i + 1}</td><td><strong>@${esc(v.name)}</strong><div class="source-note">已结算 ${v.settled_lines}/${v.lines} 行</div></td><td>${v.orders} 单 / ${v.units} 件</td><td>${money(v.actual_base, c)}</td><td>${money(v.actual_std, c)}<div class="source-note">预估 ${money(v.estimated_std, c)} · 已知实际 ${v.actual_std_known_lines} 行</div></td><td>${money(v.actual_ads, c)}<div class="source-note">预估 ${money(v.estimated_ads, c)} · 已知实际 ${v.actual_ads_known_lines} 行</div></td><td>${money(total, c)}<div class="source-note">已知部分，非完整支出</div></td></tr>`;
  });
}
function comparison() {
  const rows = report?.shops || [];
  $("compareTableBody").innerHTML = rowsHtml(rows, (r) => {
    const p = r.current,
      f = p.finance,
      c = r.shop.currency,
      o = p.orders;
    return `<tr class="clickable-row" data-shop="${esc(r.shop.id)}"><td><strong>${esc(r.shop.name)}</strong><div>${esc(r.shop.region)} · ${statuses[p.coverage.finance.status]}</div></td><td>${c}</td><td>${num(f?.order_count)}</td><td>${money(f?.net_settlement, c)}</td><td>${pct(change(f?.net_settlement, r.previous.finance?.net_settlement))}</td><td>${o?.total ? ((o.cancelled / o.total) * 100).toFixed(1) + "%" : "—"}</td><td>${money(p.affiliate?.actual_std, c)}</td><td>${money(p.affiliate?.actual_ads, c)}</td></tr>`;
  });
  $("compareTableBody")
    .querySelectorAll("[data-shop]")
    .forEach(
      (tr) =>
        (tr.onclick = () => {
          const s = shops.find((s) => s.id === tr.dataset.shop);
          state.site = s.region;
          state.store = s.id;
          filters();
          view("detail");
          load();
        }),
    );
  chart(
    "compareChart",
    "bar",
    rows.map((r) => ({
      label: r.shop.name,
      value: r.current.orders?.total ?? null,
    })),
    "当前创建日期窗口订单数",
  );
}
function insights() {
  const r = current(),
    p = r.current,
    o = p.orders,
    f = p.finance;
  const signals = [
    [
      "订单状态",
      o
        ? `本期 ${o.total} 单，已签收/完成 ${o.delivered} 单，已取消 ${o.cancelled} 单。在途和待发 ${o.in_transit} 单单独统计。`
        : "订单尚未同步。",
    ],
    [
      "结算信号",
      f
        ? `UTC账单窗口净结算 ${money(f.net_settlement, r.shop.currency)}；本期负净结算订单 ${f.negative.orders} 单。净结算不等于利润。`
        : "结算尚未同步，不能认定为未回款。",
    ],
    [
      "证据完整性",
      `${Object.values(p.coverage).some((v) => v.status !== "complete") ? "存在缺失或部分来源，先刷新或查看来源状态。" : "本次分页已完成，仍需留意平台分析滞后。"}${p.performance?.latest_available_date ? "平台截至 " + p.performance.latest_available_date + "。" : ""} 尚无SKU广告归因，不生成因果结论。`,
    ],
  ];
  $("aiInsightsGrid").innerHTML = signals
    .map(
      (v) =>
        `<div class="ai-insight-item"><strong>${v[0]}</strong><p class="source-note">${esc(v[1])}</p></div>`,
    )
    .join("");
  text("aiContextBadge", `${r.shop.region} · ${r.shop.name}`);
}
function sourceSummary() {
  const entries = report?.shops || [];
  $("sourceSummary").innerHTML = entries
    .map((row) => {
      const ready = Object.values(row.current.coverage).filter(
        (c) => c.status !== "missing",
      ).length;
      const previous = Object.values(row.previous.coverage).filter(
        (c) => c.status !== "missing",
      ).length;
      return `<div class="source-summary-shop"><strong>${esc(row.shop.name)}</strong><span>本期已读取 ${ready}/5 · 对比期 ${previous}/5</span><div class="source-chips">${Object.entries(
        row.current.coverage,
      )
        .map(
          ([source, c]) =>
            `<span class="source-chip ${c.status}" title="${c.retrieved_at ? esc(new Date(c.retrieved_at * 1000).toLocaleString("zh-CN")) : "该日期尚未获取"}">${names[source]} · ${statuses[c.status]}</span>`,
        )
        .join("")}</div></div>`;
    })
    .join("");
}
function missingData(value) {
  return value?.shops.some((row) =>
    ["current", "previous"].some((period) =>
      Object.values(row[period].coverage).some((c) => c.status === "missing"),
    ),
  );
}
function dashboardVisible() {
  return !$("dashboardView").classList.contains("hidden");
}
async function ensureData(g = generation) {
  if (
    !shops.length ||
    !dashboardVisible() ||
    (job && ["queued", "running"].includes(job.status))
  )
    return;
  const key = query();
  if (automaticRequests.has(key)) return;
  automaticRequests.add(key);
  try {
    const result = await json("/api/analytics/ensure", {
      method: "POST",
      body: JSON.stringify(params()),
    });
    if (g !== generation) {
      automaticRequests.delete(key);
      return;
    }
    if (result.job) {
      job = result.job;
      displayedProgress = -1;
      $("refreshDataBtn").disabled = true;
      void poll(job.id, g);
    } else if (result.reason === "recent_failure")
      status(
        "部分来源最近同步失败。当前保留已获取数据，请检查来源状态，或点击“刷新真实数据”重试。",
        true,
      );
  } catch (e) {
    if (g !== generation) automaticRequests.delete(key);
    if (g === generation)
      status(
        "自动同步未完成：" + e.message + "。可点击“刷新真实数据”重试。",
        true,
      );
  }
}
async function load() {
  const g = ++generation;
  controller?.abort();
  controller = new AbortController();
  clearTimeout(timer);
  job = null;
  displayedProgress = -1;
  $("refreshDataBtn").disabled = false;
  $("detailView").setAttribute("aria-busy", "true");
  $("aiChatLog").replaceChildren();
  status("正在读取所选日期的数据…");
  try {
    const value = await json("/api/analytics/dashboard?" + query(), {
      signal: controller.signal,
    });
    if (g !== generation) return;
    report = value;
    render();
    if (missingData(value))
      status(
        "所选日期有数据尚未读取，进入看板后自动同步；已获取的来源会先显示。",
      );
  } catch (e) {
    if (e.name !== "AbortError" && g === generation) {
      report = null;
      $("detailView").classList.add("hidden");
      $("compareView").classList.add("hidden");
      status(e.message, true);
    }
  } finally {
    if (g === generation) {
      $("detailView").setAttribute("aria-busy", "false");
      void ensureData(g);
    }
  }
}
async function refresh() {
  if (job && ["queued", "running"].includes(job.status)) return;
  const g = generation;
  $("refreshDataBtn").disabled = true;
  try {
    const value = await json("/api/analytics/refresh", {
      method: "POST",
      body: JSON.stringify(params()),
    });
    if (g !== generation) return;
    job = value;
    displayedProgress = -1;
    void poll(job.id, g);
  } catch (e) {
    if (g === generation) {
      status(e.message, true);
      $("refreshDataBtn").disabled = false;
    }
  }
}
async function poll(id, g) {
  if (g !== generation) return;
  try {
    const value = await json("/api/analytics/refresh/" + id);
    if (g !== generation) return;
    job = value;
    const running = ["queued", "running"].includes(job.status);
    const progress = job.progress || 0;
    if (displayedProgress !== progress || !running) {
      const snapshot = await json("/api/analytics/dashboard?" + query());
      if (g !== generation) return;
      report = snapshot;
      displayedProgress = progress;
      render();
    }
    if (running) {
      const waiting =
        job.status === "queued" && Date.now() / 1000 - job.created_at > 30;
      status(
        `数据同步 ${progress}/${job.total} · ${job.active_source ? job.active_source.shop + " / " + names[job.active_source.source] : "等待后台工作进程"}。已读取的数据正在逐项显示。${waiting ? "后台尚未领取，请确认后台进程已启动。" : ""}`,
      );
      timer = setTimeout(() => poll(id, g), 2500);
      return;
    }
    $("refreshDataBtn").disabled = false;
    if (job.status === "failed")
      status("同步任务失败，已保留成功读取的数据，请检查连接后重试。", true);
    else if (job.errors?.length)
      status(
        `同步结束，${job.errors.length} 个来源/日期失败，其余数据已显示。请查看来源状态后重试。`,
        true,
      );
    else if (job.partial_sources?.length)
      status(
        "同步已结束，部分来源尚未完成分页。请缩小日期范围或查看来源完整性。",
        true,
      );
    else
      status(
        "本期与对比期数据同步完成。下方可查看费用、SKU、地区、状态、时段、渠道及达人明细。",
      );
  } catch (e) {
    if (g === generation) {
      status(e.message + "。已获取的数据仍然保留，可点击刷新重试。", true);
      job = null;
      displayedProgress = -1;
      $("refreshDataBtn").disabled = false;
    }
  }
}
function calendar() {
  let html = "";
  for (let n = 0; n < 2; n++) {
    const month = new Date(
        state.left.getFullYear(),
        state.left.getMonth() + n,
        1,
      ),
      start = add(month, -((month.getDay() + 6) % 7));
    html += `<div><div class="cal-month-head"><button class="cal-nav-btn" data-month="-1" aria-label="上一月">‹</button><span>${month.getFullYear()}年 ${month.getMonth() + 1}月</span><button class="cal-nav-btn" data-month="1" aria-label="下一月">›</button></div><div class="cal-weekdays">${["一", "二", "三", "四", "五", "六", "日"].map((v) => `<span>${v}</span>`).join("")}</div><div class="cal-days-grid">`;
    for (let i = 0; i < 42; i++) {
      const d = add(start, i),
        k = date(d),
        valid = d.getMonth() === month.getMonth();
      html += `<button type="button" class="cal-day mono ${!valid ? "muted" : ""} ${d >= state.draftStart && d <= state.draftEnd ? "in-range" : ""} ${k === date(state.draftStart) ? "range-start" : ""} ${k === date(state.draftEnd) ? "range-end" : ""}" data-date="${k}" ${!valid || d > today ? "disabled" : ""}>${d.getDate()}</button>`;
    }
    html += "</div></div>";
  }
  $("dualCalendars").innerHTML = html;
  text(
    "rangePreviewText",
    `${date(state.draftStart)} — ${date(state.draftEnd)}（最多31天）`,
  );
}
function commit(start, end) {
  if (
    !start ||
    !end ||
    end < start ||
    (end - start) / 86400000 >= 31 ||
    end > today
  ) {
    status("请选择有效的1至31天日期范围，且不晚于今天。", true);
    return false;
  }
  state.start = start;
  state.end = end;
  load();
  return true;
}
async function openDetails(kind, field = null, zero = false, page = 1) {
  const r = current();
  if (!r) return;
  const v = ++detailVersion;
  detail = { kind, field, zero, page };
  if (!$("detailDialog").open) $("detailDialog").showModal();
  text(
    "detailTitle",
    kind === "expense"
      ? "费用对应订单"
      : kind === "sku"
        ? "SKU关联订单"
        : "结算流水明细",
  );
  text("detailContent", "正在读取…");
  try {
    const q = new URLSearchParams({
      ...params(),
      kind,
      selected_shop: r.shop.id,
      page: String(page),
      page_size: "30",
      zero: String(zero),
      ...(field ? { field } : {}),
    });
    const d = await json("/api/analytics/details?" + q);
    if (v !== detailVersion || !$("detailDialog").open) return;
    const cols =
      kind === "expense"
        ? ["order_id", "amount", "currency", "revenue"]
        : kind === "settlement"
          ? [
              "order_id",
              "type",
              "settlement_amount",
              "revenue_amount",
              "fee_tax_amount",
              "currency",
            ]
          : ["id", "status", "create_time", "payment", "line_items"];
    const labels = {
      order_id: "订单ID",
      amount: "费用原值",
      currency: "币种",
      revenue: "结算营收",
      id: "订单ID",
      status: "状态",
      create_time: "创建时间",
      payment: "买家支付",
      line_items: "商品明细",
      type: "流水类型",
      settlement_amount: "净结算额",
      revenue_amount: "营收",
      fee_tax_amount: "费用税费",
    };
    $("detailContent").innerHTML =
      `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th>${labels[c]}</th>`).join("")}</tr></thead><tbody>${rowsHtml(d.rows, (row) => `<tr>${cols.map((c) => `<td>${esc(c === "create_time" ? new Date(row[c] * 1000).toLocaleString("zh-CN", { timeZone: r.shop.timezone }) : typeof row[c] === "object" ? JSON.stringify(row[c]) : row[c])}</td>`).join("")}</tr>`)}</tbody></table></div>`;
    text(
      "detailPage",
      `${page}/${Math.max(1, Math.ceil(d.total / 30))} 页 · ${d.total} 条 · ${statuses[d.coverage]}`,
    );
    $("detailPrev").disabled = page <= 1;
    $("detailNext").disabled = page * 30 >= d.total;
  } catch (e) {
    text("detailContent", e.message);
  }
}
function message(role, value) {
  const el = document.createElement("div");
  el.className = "ai-chat-message " + role;
  el.textContent = (role === "user" ? "你：" : "助手：") + value;
  $("aiChatLog").append(el);
  el.scrollIntoView({ block: "nearest" });
  return el;
}
async function ask(question) {
  if (chatBusy || !question.trim()) return;
  const g = generation;
  chatBusy = true;
  message("user", question);
  $("aiChatInput").value = "";
  const answer = message("bot", "正在解读当前筛选…");
  try {
    const d = await json("/api/analytics/ask", {
      method: "POST",
      body: JSON.stringify({ ...params(), question }),
    });
    if (g === generation)
      answer.innerHTML =
        "助手：" +
        esc(d.answer).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>") +
        `<p class="source-note">来源：${d.evidence.map((e) => esc(names[e.source]) + " · " + statuses[e.status]).join(" / ")}</p>`;
  } catch (e) {
    if (g === generation) answer.textContent = e.message;
  } finally {
    chatBusy = false;
  }
}
$("siteSelect").onchange = (e) => {
  state.site = e.target.value;
  state.store = "ALL";
  filters();
  load();
};
$("storeSelect").onchange = (e) => {
  state.store = e.target.value;
  load();
};
$("refreshDataBtn").onclick = refresh;
$("openPickerBtn").onclick = (e) => {
  e.stopPropagation();
  state.draftStart = state.start;
  state.draftEnd = state.end;
  state.first = true;
  $("datePopover").classList.toggle("hidden");
  calendar();
};
$("dualCalendars").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled) return;
  if (b.dataset.month) {
    state.left = new Date(
      state.left.getFullYear(),
      state.left.getMonth() + Number(b.dataset.month),
      1,
    );
    calendar();
    return;
  }
  const d = parse(b.dataset.date);
  if (!d) return;
  if (state.first) {
    state.draftStart = d;
    state.draftEnd = d;
  } else if (d < state.draftStart) {
    state.draftEnd = state.draftStart;
    state.draftStart = d;
  } else state.draftEnd = d;
  state.first = !state.first;
  calendar();
};
document.querySelectorAll("[data-preset]").forEach(
  (b) =>
    (b.onclick = () => {
      const p = b.dataset.preset;
      state.draftEnd = p === "yesterday" ? yesterday : today;
      state.draftStart = add(
        state.draftEnd,
        p === "last7" ? -6 : p === "last30" ? -29 : 0,
      );
      document
        .querySelectorAll("[data-preset]")
        .forEach((v) => v.classList.toggle("active", v === b));
      calendar();
    }),
);
$("customRangeBtn").onclick = () => {
  state.first = true;
  calendar();
};
$("applyRangeBtn").onclick = () => {
  if (commit(state.draftStart, state.draftEnd))
    $("datePopover").classList.add("hidden");
};
for (const id of ["startDateInput", "endDateInput"])
  $(id).onchange = () =>
    commit(parse($("startDateInput").value), parse($("endDateInput").value));
$("resetDefaultBtn").onclick = () => {
  state.draftStart = today;
  state.draftEnd = today;
  commit(today, today);
};
document.addEventListener("click", (e) => {
  if (!$("dateStripWrap").contains(e.target))
    $("datePopover").classList.add("hidden");
});
document.querySelectorAll("#viewModeGroup button").forEach(
  (b) =>
    (b.onclick = () => {
      if (b.dataset.mode === "detail" && !current()) {
        status("请选择一个站点/店铺查看明细；跨站点请使用横向对比。");
        return;
      }
      view(b.dataset.mode);
    }),
);
document.querySelectorAll("#skuFilterGroup button").forEach(
  (b) =>
    (b.onclick = () => {
      state.sku = b.dataset.skuFilter;
      document
        .querySelectorAll("#skuFilterGroup button")
        .forEach((v) => v.classList.toggle("active", v === b));
      skus();
    }),
);
document.querySelectorAll("#creatorSortGroup button").forEach(
  (b) =>
    (b.onclick = () => {
      state.sort = b.dataset.sort;
      document
        .querySelectorAll("#creatorSortGroup button")
        .forEach((v) => v.classList.toggle("active", v === b));
      creators();
    }),
);
for (const [id, compact] of [
  ["densityComfortable", false],
  ["densityCompact", true],
])
  $(id).onclick = () => {
    document.body.classList.toggle("density-compact", compact);
    $("densityComfortable").classList.toggle("active", !compact);
    $("densityCompact").classList.toggle("active", compact);
  };
$("closeDetails").onclick = () => {
  $("detailDialog").close();
  detailVersion++;
};
$("detailPrev").onclick = () =>
  openDetails(detail.kind, detail.field, detail.zero, detail.page - 1);
$("detailNext").onclick = () =>
  openDetails(detail.kind, detail.field, detail.zero, detail.page + 1);
$("aiChatForm").onsubmit = (e) => {
  e.preventDefault();
  ask($("aiChatInput").value);
};
$("aiQuickPrompts")
  .querySelectorAll("button")
  .forEach((b) => (b.onclick = () => ask(b.dataset.q)));
$("clearAiChatBtn").onclick = () => {
  $("aiChatLog").replaceChildren();
};
$("exportCsvBtn").onclick = async () => {
  try {
    const r = await api("/api/analytics/export?" + query()),
      url = URL.createObjectURL(await r.blob()),
      a = document.createElement("a");
    a.href = url;
    a.download = `shop-analytics-${state.site}-${date(state.start)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    status(e.message, true);
  }
};
window.addEventListener("abrain:dashboard-visible", () => {
  void ensureData(generation);
});

initializeHub({
  json,
  resizeCharts: () => Object.values(charts).forEach((chart) => chart.resize()),
});

// Refresh cached snapshots without silently issuing third-party calls.
setInterval(async () => {
  if (document.hidden || (job && ["queued", "running"].includes(job.status)))
    return;
  const g = generation;
  try {
    const d = await json("/api/analytics/dashboard?" + query());
    if (g === generation) {
      report = d;
      render();
      if (missingData(d)) void ensureData(g);
    }
  } catch {
    /* keep the visible snapshot */
  }
}, 60000);
(async () => {
  try {
    shops = (await json("/api/analytics/shops")).shops;
    if (!shops.length) {
      status("尚无已授权Shop店铺，请返回运营助手完成连接。", true);
      return;
    }
    if (!shops.some((s) => s.region === state.site))
      state.site = shops[0].region;
    filters();
    calendar();
    await load();
  } catch (e) {
    status(e.message, true);
  }
})();
