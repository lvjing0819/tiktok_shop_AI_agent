import { useState, useEffect } from "react";
import type { Api, Run } from "./types";
export type { Api } from "./types";
import {
  Icon,
  Metric,
  StatusBadge,
  EmptyState,
  Drawer,
  RoleMark,
  formatTime,
  money,
  roleNames,
} from "./ui";
import { OrderChart } from "./Charts";
import PricingPanel from "./PricingPanel";
type Warehouse = { id: string; name: string; country: string; kind: string };
type Product = { id: string; name: string; facts: string };
type SKU = { id: string; product_id: string; code: string; name: string };
type Stock = {
  warehouse_id: string;
  warehouse: string;
  country: string;
  kind: string;
  sku_id: string;
  sku: string;
  product_id: string;
  on_hand: number | null;
  warehouse_reserved: number | null;
  local_reserved: number;
  safety_stock: number | null;
  available: number | null;
  revision: number;
  updated_at: number | null;
  stale: boolean;
  over_reserved: boolean;
};
type Order = {
  shop_id: string;
  order_id: string;
  status: string;
  currency: string;
  total_amount: string | null;
  create_time: number;
  synced_at: number;
  costs_complete: boolean;
  finance_revision: number;
  estimated_contribution: string | null;
  settled_profit: string | null;
  finance_input?: Record<string, any>;
};
type Shop = { id: string; name: string; region: string };
const financeLabels = {
  procurement: "采购成本",
  logistics: "物流成本",
  advertising: "广告成本",
  platform_fee: "平台费用",
  refund: "退款金额",
  other: "其他成本",
  settled_net: "净结算额（选填）",
  source_note: "成本与结算依据",
};
const emptyCosts = () =>
  Object.fromEntries(Object.keys(financeLabels).map((k) => [k, ""]));
export default function BusinessPanel({
  api,
  initialTab,
  runs = [],
  onNavigate = () => {},
}: {
  api: Api;
  initialTab: string;
  runs?: Run[];
  onNavigate?: (view: string, run?: string) => void;
}) {
  const [data, setData] = useState<any>(null),
    [catalog, setCatalog] = useState<{
      products: Product[];
      skus: SKU[];
      pricing: any[];
    }>({ products: [], skus: [], pricing: [] }),
    [shops, setShops] = useState<Shop[]>([]),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [drawer, setDrawer] = useState("");
  const [shopFilter, setShopFilter] = useState(""),
    [dateRange, setDateRange] = useState("7"),
    [search, setSearch] = useState(""),
    [statusFilter, setStatusFilter] = useState(""),
    [warehouseFilter, setWarehouseFilter] = useState(""),
    [page, setPage] = useState(1),
    [currency, setCurrency] = useState("");
  const [warehouseName, setWarehouseName] = useState(""),
    [country, setCountry] = useState("CN"),
    [productName, setProductName] = useState(""),
    [facts, setFacts] = useState(""),
    [productId, setProductId] = useState(""),
    [skuCode, setSkuCode] = useState("");
  const [stockWarehouse, setStockWarehouse] = useState(""),
    [stockSku, setStockSku] = useState(""),
    [onHand, setOnHand] = useState(""),
    [reserved, setReserved] = useState("0"),
    [safety, setSafety] = useState("0"),
    [stockRevision, setStockRevision] = useState(0),
    [reason, setReason] = useState("");
  const [shopId, setShopId] = useState(""),
    [startDate, setStartDate] = useState(""),
    [endDate, setEndDate] = useState(""),
    [pollSeconds, setPollSeconds] = useState("300"),
    [pollDays, setPollDays] = useState("7");
  const [financeOrder, setFinanceOrder] = useState(""),
    [financeFields, setFinanceFields] =
      useState<Record<string, string>>(emptyCosts),
    [complete, setComplete] = useState(false),
    [financeRevision, setFinanceRevision] = useState(0);
  async function refresh() {
    const [d, c] = await Promise.all([
      api("/business/dashboard"),
      api("/business/catalog"),
    ]);
    setData(d);
    setCatalog(c);
  }
  useEffect(() => {
    let live = true;
    async function load() {
      try {
        const [d, c] = await Promise.all([
          api("/business/dashboard"),
          api("/business/catalog"),
        ]);
        if (live) {
          setData(d);
          setCatalog(c);
        }
      } catch (e) {
        if (live) setError(String(e));
      }
    }
    load();
    const timer = setInterval(load, 5000);
    api("/shop/shops")
      .then((d) => {
        if (live) {
          setShops(d.shops);
          if (d.shops.length) setShopId(d.shops[0].id);
        }
      })
      .catch(() => {});
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api]);
  useEffect(() => {
    setPage(1);
    setSearch("");
    setStatusFilter("");
  }, [initialTab]);
  useEffect(
    () => setPage(1),
    [shopFilter, dateRange, search, statusFilter, warehouseFilter, currency],
  );
  useEffect(() => {
    if (data && !currency)
      setCurrency(Object.keys(data.finance.by_currency)[0] || "");
  }, [data, currency]);
  async function act(fn: () => Promise<void>, close = false) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await fn();
      await refresh();
      if (close) setDrawer("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const warehouses: Warehouse[] = data?.inventory.warehouses || [],
    allStock: Stock[] = data?.inventory.rows || [],
    orders: Order[] = data?.orders.rows || [];
  const shopName = (id: string) =>
      shops.find((s) => s.id === id)?.name || "店铺 " + id.slice(-6),
    region = (id: string) => shops.find((s) => s.id === id)?.region || "";
  const shopIds = Array.from(new Set(orders.map((o) => o.shop_id)));
  const cutoff = (() => {
    if (dateRange === "all") return 0;
    const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
    return (
      Date.parse(today + "T00:00:00+08:00") / 1000 -
      (Number(dateRange) - 1) * 86400
    );
  })();
  const filtered = orders
    .filter(
      (o) =>
        (!shopFilter || o.shop_id === shopFilter) &&
        o.create_time >= cutoff &&
        (!statusFilter || o.status === statusFilter) &&
        (!search ||
          o.order_id.includes(search) ||
          shopName(o.shop_id).toLowerCase().includes(search.toLowerCase())),
    )
    .sort((a, b) => b.create_time - a.create_time);
  const filteredStock = allStock.filter(
    (r) =>
      (!warehouseFilter || r.warehouse_id === warehouseFilter) &&
      (!search ||
        r.sku.toLowerCase().includes(search.toLowerCase()) ||
        r.warehouse.includes(search)),
  );
  const financeOrders = orders
    .filter(
      (o) =>
        o.currency === currency && (!search || o.order_id.includes(search)),
    )
    .sort((a, b) => b.create_time - a.create_time);
  const knownStock = allStock.filter((r) => r.on_hand !== null),
    knownAvailable = knownStock.length
      ? knownStock.reduce((s, r) => s + (r.available || 0), 0)
      : null;
  const g = data?.finance.by_currency[currency],
    polls: any[] = data?.orders.polling || [],
    syncs: any[] = data?.orders.sync || [];
  const latestSync = syncs.length
    ? Math.max(...syncs.map((s) => s.synced_at))
    : null;
  const attention = runs.filter((r) =>
    ["waiting_input", "waiting_approval", "failed", "blocked"].includes(
      r.status,
    ),
  );
  const titles: Record<string, [string, string, string]> = {
    overview: [
      "OPERATIONS OVERVIEW",
      "经营总览",
      "从真实业务快照，了解你的店铺与待办。",
    ],
    orders: [
      "ORDER MANAGEMENT",
      "订单中心",
      "集中查看已同步订单，追踪履约与数据更新。",
    ],
    inventory: [
      "INVENTORY CONTROL",
      "库存管理",
      "统一管理大陆仓与海外仓，实物库存独立记账。",
    ],
    finance: [
      "FINANCIAL INSIGHTS",
      "财务分析",
      "区分订单金额、成本与结算，让每个利润数字有依据。",
    ],
  };
  const heading = titles[initialTab] || titles.overview;
  function editStock(w: string, s: string) {
    const r = allStock.find((r) => r.warehouse_id === w && r.sku_id === s);
    setStockWarehouse(w);
    setStockSku(s);
    setStockRevision(r?.revision || 0);
    setOnHand(r?.on_hand == null ? "" : String(r.on_hand));
    setReserved(String(r?.warehouse_reserved || 0));
    setSafety(String(r?.safety_stock || 0));
    setReason("");
  }
  function editFinance(order: Order) {
    setFinanceOrder(order.shop_id + ":" + order.order_id);
    setFinanceFields(
      Object.fromEntries(
        Object.keys(financeLabels).map((k) => [
          k,
          String(order.finance_input?.[k] ?? ""),
        ]),
      ),
    );
    setComplete(order.costs_complete);
    setFinanceRevision(order.finance_revision);
    setDrawer("finance");
  }
  const selectedOrder = orders.find(
    (o) => o.shop_id + ":" + o.order_id === financeOrder,
  );
  function tableFooter(count: number) {
    const pages = Math.max(1, Math.ceil(count / 15));
    return (
      <div className="table-footer">
        <span>共 {count.toLocaleString()} 条 · 每页 15 条</span>
        <div className="pagination">
          <button
            className="icon-button"
            aria-label="上一页"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
          >
            <Icon name="chevron" className="rotate" size={14} />
          </button>
          <span>
            {Math.min(page, pages)} / {pages}
          </span>
          <button
            className="icon-button"
            aria-label="下一页"
            disabled={page >= pages}
            onClick={() => setPage(page + 1)}
          >
            <Icon name="chevron" size={14} />
          </button>
        </div>
      </div>
    );
  }
  const pageItems = <T,>(rows: T[]) =>
    rows.slice(
      (Math.min(page, Math.max(1, Math.ceil(rows.length / 15))) - 1) * 15,
      Math.min(page, Math.max(1, Math.ceil(rows.length / 15))) * 15,
    );
  return (
    <section className="business-panel">
      <div className="page-heading">
        <div>
          <div className="eyebrow">{heading[0]}</div>
          <h1>{heading[1]}</h1>
          <p>{heading[2]}</p>
        </div>
        <div className="heading-actions">
          {initialTab === "inventory" ? (
            <>
              <button
                className="secondary"
                onClick={() => setDrawer("catalog")}
              >
                <Icon name="box" size={16} />
                管理档案
              </button>
              <button
                onClick={() => {
                  editStock(warehouses[0]?.id || "", catalog.skus[0]?.id || "");
                  setDrawer("stock");
                }}
              >
                <Icon name="plus" size={16} />
                录入盘点
              </button>
            </>
          ) : initialTab === "finance" ? (
            <button
              disabled={!orders.length}
              onClick={() =>
                editFinance(orders.find((o) => !o.costs_complete) || orders[0])
              }
            >
              <Icon name="plus" size={16} />
              录入成本
            </button>
          ) : (
            <button className="secondary" onClick={() => setDrawer("sync")}>
              <Icon name="refresh" size={16} />
              同步管理
            </button>
          )}
        </div>
      </div>
      {error && (
        <div className="toast error" role="alert">
          <Icon name="alert" />
          <span>{error.replace(/^Error: /, "")}</span>
          <button
            className="icon-button"
            aria-label="关闭错误"
            onClick={() => setError("")}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      )}
      {notice && (
        <div className="toast success" role="status">
          <Icon name="check" />
          {notice}
        </div>
      )}
      {!data ? (
        <div className="skeleton-page">
          <div className="metric-grid">
            {[1, 2, 3, 4].map((i) => (
              <div className="skeleton skeleton-metric" key={i} />
            ))}
          </div>
          <div className="skeleton skeleton-chart" />
        </div>
      ) : (
        <>
          {(initialTab === "overview" || initialTab === "orders") && (
            <div className="data-toolbar">
              <div className="toolbar-filters">
                <select
                  aria-label="按店铺筛选"
                  value={shopFilter}
                  onChange={(e) => setShopFilter(e.target.value)}
                >
                  <option value="">全部店铺</option>
                  {shopIds.map((id) => (
                    <option key={id} value={id}>
                      {shopName(id)}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="按创建日期筛选"
                  value={dateRange}
                  onChange={(e) => setDateRange(e.target.value)}
                >
                  <option value="7">最近 7 天</option>
                  <option value="30">最近 30 天</option>
                  <option value="all">全部已保存数据</option>
                </select>
                <span className="small-pill subtle">
                  <Icon name="clock" size={12} />
                  创建时间 · 北京时间
                </span>
              </div>
              <span className="sync-caption">
                <i
                  className={
                    latestSync && Date.now() / 1000 - latestSync < 900
                      ? "live-dot"
                      : "quiet-dot"
                  }
                />
                同步于 {formatTime(latestSync, true)}
              </span>
            </div>
          )}
          {initialTab === "overview" && (
            <>
              <div className="metric-grid">
                <Metric
                  label="已同步订单"
                  value={filtered.length.toLocaleString()}
                  note={`${new Set(filtered.map((o) => o.shop_id)).size} 个店铺 · 所选创建时间范围`}
                  icon="orders"
                />
                <Metric
                  label="待履约订单"
                  value={
                    filtered.filter((o) =>
                      [
                        "AWAITING_SHIPMENT",
                        "PARTIALLY_SHIPPING",
                        "AWAITING_COLLECTION",
                      ].includes(o.status),
                    ).length
                  }
                  note="待发货、部分发货与待揽收"
                  icon="box"
                />
                <Metric
                  label="实物可售库存"
                  value={
                    knownAvailable === null
                      ? "—"
                      : knownAvailable.toLocaleString()
                  }
                  note={
                    knownStock.length
                      ? `${warehouses.length} 个仓库 · 仅汇总已知库存`
                      : "尚未建立实物库存记录"
                  }
                  icon="grid"
                />
                <Metric
                  label="待处理运营任务"
                  value={attention.length}
                  note={
                    attention.length
                      ? "含待补充、待审批与受阻任务"
                      : "当前没有待处理任务"
                  }
                  icon="tasks"
                  tone={attention.length ? "attention" : ""}
                />
              </div>
              <div className="overview-middle">
                <article className="chart-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>订单分布</h2>
                      <p>近 7 天已保存订单的创建日期分布</p>
                    </div>
                    <span className="chart-legend">
                      <i />
                      订单数
                    </span>
                  </div>
                  {orders.length ? (
                    <OrderChart
                      orders={orders.filter(
                        (o) => !shopFilter || o.shop_id === shopFilter,
                      )}
                    />
                  ) : (
                    <EmptyState
                      icon="orders"
                      title="同步后查看订单分布"
                      description="从授权店铺读取真实订单后，图表会在这里更新。"
                    />
                  )}
                  <div className="chart-footnote">
                    按北京时间展示快照 ·
                    当前日尚未结束，图表不代表全店完整订单量
                  </div>
                </article>
                <article className="attention-panel">
                  <div className="panel-heading">
                    <h2>需要关注</h2>
                    <span className="small-pill">待办</span>
                  </div>
                  <button
                    className="attention-row"
                    onClick={() => onNavigate("finance")}
                  >
                    <span className="attention-symbol amber">
                      <Icon name="wallet" size={19} />
                    </span>
                    <span>
                      <strong>
                        {data.finance.missing_cost_orders
                          ? `${data.finance.missing_cost_orders} 笔订单待核对成本`
                          : "订单成本核对"}
                      </strong>
                      <small>
                        {data.finance.missing_cost_orders
                          ? "成本不完整，利润暂不计算"
                          : "查看成本与净结算口径"}
                      </small>
                    </span>
                    <Icon name="chevron" size={14} />
                  </button>
                  <button
                    className="attention-row"
                    onClick={() => onNavigate("inventory")}
                  >
                    <span className="attention-symbol blue">
                      <Icon name="box" size={19} />
                    </span>
                    <span>
                      <strong>
                        {!knownStock.length
                          ? "实物库存待建档"
                          : `${allStock.filter((r) => r.stale || r.on_hand === null).length} 项库存待确认`}
                      </strong>
                      <small>
                        {!knownStock.length
                          ? "连接仓库前，可先录入盘点记录"
                          : "未知和过期库存需要人工复核"}
                      </small>
                    </span>
                    <Icon name="chevron" size={14} />
                  </button>
                  <button
                    className="attention-row"
                    onClick={() => onNavigate("operations")}
                  >
                    <span className="attention-symbol green">
                      <Icon name="tasks" size={19} />
                    </span>
                    <span>
                      <strong>
                        {attention.length
                          ? `${attention.length} 个运营任务待处理`
                          : "查看运营任务交付"}
                      </strong>
                      <small>确认资料、审核计划与查看结果</small>
                    </span>
                    <Icon name="chevron" size={14} />
                  </button>
                  <div className="attention-note">
                    <Icon name="check" size={14} />
                    仅依据当前已保存业务记录
                  </div>
                </article>
              </div>
              <article className="recent-tasks-panel">
                <div className="panel-heading">
                  <div>
                    <h2>最近运营任务</h2>
                    <p>从计划到交付，保留每一步状态。</p>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => onNavigate("operations")}
                  >
                    查看全部
                    <Icon name="arrow" size={14} />
                  </button>
                </div>
                {!runs.length ? (
                  <EmptyState
                    icon="tasks"
                    title="还没有运营任务"
                    action={
                      <button
                        className="secondary"
                        onClick={() => onNavigate("chat")}
                      >
                        交给 AI 一个任务
                        <Icon name="arrow" size={14} />
                      </button>
                    }
                  />
                ) : (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>任务</th>
                          <th>参与团队</th>
                          <th>创建时间</th>
                          <th>状态</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {runs.slice(0, 5).map((r) => (
                          <tr key={r.id}>
                            <td>
                              <button
                                className="table-link"
                                onClick={() => onNavigate("operations", r.id)}
                              >
                                {r.plan.title}
                              </button>
                              <small className="cell-subtitle">
                                {r.plan.steps.length} 个计划步骤
                              </small>
                            </td>
                            <td>
                              <div className="role-stack">
                                {Array.from(
                                  new Set(r.plan.steps.map((s) => s.agent)),
                                ).map((role) => (
                                  <span key={role} title={roleNames[role]}>
                                    <RoleMark role={role} />
                                  </span>
                                ))}
                              </div>
                            </td>
                            <td className="muted nowrap">
                              {formatTime(r.created_at, true)}
                            </td>
                            <td>
                              <StatusBadge status={r.status} />
                            </td>
                            <td>
                              <button
                                className="icon-button"
                                aria-label={"查看任务 " + r.plan.title}
                                onClick={() => onNavigate("operations", r.id)}
                              >
                                <Icon name="arrow" size={15} />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </article>
              <div className="source-strip">
                <span>
                  <Icon name="link" size={15} />
                  数据来源
                </span>
                <span>TikTok Shop 订单快照</span>
                <span>人工实物库存账</span>
                <span>人工成本记录</span>
                <span className="source-strip-end">
                  看板每 5 秒刷新已保存数据
                </span>
              </div>
            </>
          )}
          {initialTab === "orders" && (
            <>
              <div className="metric-grid">
                <Metric
                  label="已保存订单"
                  value={filtered.length.toLocaleString()}
                  note="所选店铺与创建时间范围"
                  icon="orders"
                />
                <Metric
                  label="待发货 / 待揽收"
                  value={
                    filtered.filter((o) =>
                      [
                        "AWAITING_SHIPMENT",
                        "PARTIALLY_SHIPPING",
                        "AWAITING_COLLECTION",
                      ].includes(o.status),
                    ).length
                  }
                  note="按当前订单状态统计"
                  icon="box"
                />
                <Metric
                  label="运输中"
                  value={
                    filtered.filter((o) => o.status === "IN_TRANSIT").length
                  }
                  note="等待物流妥投"
                  icon="globe"
                />
                <Metric
                  label="已送达 / 已完成"
                  value={
                    filtered.filter((o) =>
                      ["DELIVERED", "COMPLETED"].includes(o.status),
                    ).length
                  }
                  note="不代表订单已完成财务结算"
                  icon="check"
                />
              </div>
              <article className="data-table-panel">
                <div className="table-toolbar">
                  <div className="search-input">
                    <Icon name="search" size={16} />
                    <input
                      aria-label="搜索订单"
                      placeholder="搜索订单号或店铺名称"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                    />
                  </div>
                  <select
                    aria-label="按订单状态筛选"
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                  >
                    <option value="">全部状态</option>
                    {Array.from(new Set(orders.map((o) => o.status))).map(
                      (s) => (
                        <option key={s} value={s}>
                          {(
                            {
                              UNPAID: "待付款",
                              AWAITING_SHIPMENT: "待发货",
                              AWAITING_COLLECTION: "待揽收",
                              IN_TRANSIT: "运输中",
                              DELIVERED: "已送达",
                              COMPLETED: "已完成",
                              CANCELLED: "已取消",
                            } as Record<string, string>
                          )[s] || s}
                        </option>
                      ),
                    )}
                  </select>
                </div>
                {filtered.length ? (
                  <>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>订单号 / 创建时间</th>
                            <th>店铺</th>
                            <th>支付总额</th>
                            <th>订单状态</th>
                            <th>最近同步</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {pageItems(filtered).map((o) => (
                            <tr key={o.shop_id + o.order_id}>
                              <td>
                                <span className="order-id">{o.order_id}</span>
                                <small className="cell-subtitle">
                                  {formatTime(o.create_time, true)}
                                </small>
                              </td>
                              <td>
                                <div className="shop-cell">
                                  <span className="country-mark">
                                    {region(o.shop_id) || "TK"}
                                  </span>
                                  <span>{shopName(o.shop_id)}</span>
                                </div>
                              </td>
                              <td className="numeric">
                                {money(o.total_amount)}
                                <small className="currency-code">
                                  {o.currency}
                                </small>
                              </td>
                              <td>
                                <StatusBadge status={o.status} />
                              </td>
                              <td className="muted nowrap">
                                {formatTime(o.synced_at, true)}
                              </td>
                              <td>
                                <button
                                  className="text-button"
                                  onClick={() => editFinance(o)}
                                >
                                  核对成本
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {tableFooter(filtered.length)}
                  </>
                ) : (
                  <EmptyState
                    icon="orders"
                    title={
                      orders.length
                        ? "没有符合筛选条件的订单"
                        : "还没有已同步订单"
                    }
                    description={
                      orders.length
                        ? "试试调整店铺、日期或搜索条件。"
                        : "连接已授权店铺并选择日期，即可读取真实订单。"
                    }
                    action={
                      !orders.length && (
                        <button onClick={() => setDrawer("sync")}>
                          同步订单
                          <Icon name="arrow" size={14} />
                        </button>
                      )
                    }
                  />
                )}
              </article>
              <p className="data-footnote">
                <Icon name="clock" size={14} />
                仅展示已保存快照。订单金额为平台支付字段，不等于结算收入或利润。
                <button
                  className="text-button"
                  onClick={() => setDrawer("sync")}
                >
                  查看同步范围
                </button>
              </p>
            </>
          )}
          {initialTab === "inventory" && (
            <>
              <div className="metric-grid">
                <Metric
                  label="已建档仓库"
                  value={warehouses.length}
                  note={`${warehouses.filter((w) => w.kind === "mainland").length} 个大陆仓 · ${warehouses.filter((w) => w.kind === "overseas").length} 个海外仓`}
                  icon="globe"
                />
                <Metric
                  label="内部 SKU"
                  value={catalog.skus.length}
                  note={`${catalog.products.length} 个商品档案`}
                  icon="box"
                />
                <Metric
                  label="已知可售库存"
                  value={
                    knownAvailable === null
                      ? "—"
                      : knownAvailable.toLocaleString()
                  }
                  note={
                    knownStock.length
                      ? "已扣除占用与安全库存"
                      : "尚无已确认盘点记录"
                  }
                  icon="grid"
                />
                <Metric
                  label="待复核库存项"
                  value={
                    allStock.filter(
                      (r) => r.stale || r.on_hand === null || r.over_reserved,
                    ).length
                  }
                  note="未知、超过24小时或占用异常"
                  icon="alert"
                />
              </div>
              <article className="data-table-panel">
                <div className="table-toolbar">
                  <div className="search-input">
                    <Icon name="search" size={16} />
                    <input
                      aria-label="搜索库存"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="搜索 SKU 或仓库"
                    />
                  </div>
                  <div className="button-group">
                    <select
                      aria-label="筛选仓库"
                      value={warehouseFilter}
                      onChange={(e) => setWarehouseFilter(e.target.value)}
                    >
                      <option value="">全部仓库</option>
                      {warehouses.map((w) => (
                        <option key={w.id} value={w.id}>
                          {w.name}（{w.country}）
                        </option>
                      ))}
                    </select>
                    <button
                      className="secondary"
                      onClick={() => setDrawer("pricing")}
                    >
                      定价规则
                    </button>
                  </div>
                </div>
                {filteredStock.length ? (
                  <>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>商品 / SKU</th>
                            <th>仓库</th>
                            <th className="numeric">实物库存</th>
                            <th className="numeric">仓库占用</th>
                            <th className="numeric">本地占用</th>
                            <th className="numeric">安全库存</th>
                            <th className="numeric">可售</th>
                            <th>盘点状态</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {pageItems(filteredStock).map((r) => (
                            <tr key={r.warehouse_id + r.sku_id}>
                              <td>
                                <strong className="cell-name">
                                  {catalog.products.find(
                                    (p) => p.id === r.product_id,
                                  )?.name || r.sku}
                                </strong>
                                <small className="cell-subtitle">{r.sku}</small>
                              </td>
                              <td>
                                {r.warehouse}
                                <small className="cell-subtitle">
                                  {r.country} ·{" "}
                                  {r.kind === "mainland" ? "大陆仓" : "海外仓"}
                                </small>
                              </td>
                              <td className="numeric">{r.on_hand ?? "—"}</td>
                              <td className="numeric">
                                {r.warehouse_reserved ?? "—"}
                              </td>
                              <td className="numeric">{r.local_reserved}</td>
                              <td className="numeric">
                                {r.safety_stock ?? "—"}
                              </td>
                              <td className="numeric inventory-available">
                                {r.available ?? "—"}
                              </td>
                              <td>
                                <StatusBadge
                                  status={
                                    r.on_hand === null
                                      ? "waiting_input"
                                      : r.stale || r.over_reserved
                                        ? "partial"
                                        : "completed"
                                  }
                                  label={
                                    r.on_hand === null
                                      ? "未盘点"
                                      : r.over_reserved
                                        ? "占用异常"
                                        : r.stale
                                          ? "待复核"
                                          : "已记录"
                                  }
                                />
                              </td>
                              <td>
                                <button
                                  className="text-button"
                                  onClick={() => {
                                    editStock(r.warehouse_id, r.sku_id);
                                    setDrawer("stock");
                                  }}
                                >
                                  盘点
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {tableFooter(filteredStock.length)}
                  </>
                ) : (
                  <EmptyState
                    icon="box"
                    title={
                      search || warehouseFilter
                        ? "没有匹配的库存项"
                        : "从实物库存，建立可信的库存账。"
                    }
                    description="先建立仓库和内部 SKU，再录入盘点数量。未确认的库存会保持未知。"
                    action={
                      <button onClick={() => setDrawer("catalog")}>
                        <Icon name="plus" size={15} />
                        建立库存档案
                      </button>
                    }
                  />
                )}
              </article>
              <div className="inventory-explainer">
                <Icon name="box" size={20} />
                <div>
                  <strong>
                    实物可售 = 实物数量 − 仓库占用 − 本地占用 − 安全库存
                  </strong>
                  <p>
                    TikTok 展示库存不作为实物库存。仓库 API
                    连接暂留空，当前以人工确认记录为准。
                  </p>
                </div>
              </div>
              <div className="grid">
                <article>
                  <div className="panel-heading">
                    <h2>在途调拨</h2>
                    <span className="small-pill">
                      {data.inventory.transfers.length}
                    </span>
                  </div>
                  {!data.inventory.transfers.length ? (
                    <p className="quiet-empty">暂无调拨记录</p>
                  ) : (
                    data.inventory.transfers.map((t: any) => (
                      <div className="record-row" key={t.id}>
                        <span>
                          {warehouses.find((w) => w.id === t.source_id)?.name} →{" "}
                          {
                            warehouses.find((w) => w.id === t.destination_id)
                              ?.name
                          }
                          <small>{t.quantity} 件</small>
                        </span>
                        <StatusBadge status={t.state} />
                      </div>
                    ))
                  )}
                </article>
                <article>
                  <div className="panel-heading">
                    <h2>订单占用</h2>
                    <span className="small-pill">
                      {data.inventory.reservations.length}
                    </span>
                  </div>
                  {!data.inventory.reservations.length ? (
                    <p className="quiet-empty">暂无已记录占用</p>
                  ) : (
                    data.inventory.reservations.map((r: any) => (
                      <div className="record-row" key={r.id}>
                        <span>
                          {r.order_ref}
                          <small>{r.quantity} 件</small>
                        </span>
                        <StatusBadge status={r.state} />
                      </div>
                    ))
                  )}
                </article>
              </div>
            </>
          )}
          {initialTab === "finance" && (
            <>
              <div className="data-toolbar">
                <div
                  className="currency-tabs"
                  role="group"
                  aria-label="财务币种"
                >
                  {Object.keys(data.finance.by_currency).map((c) => (
                    <button
                      className={currency === c ? "active" : ""}
                      onClick={() => setCurrency(c)}
                      key={c}
                    >
                      {c}
                    </button>
                  ))}
                </div>
                <span className="sync-caption">
                  <Icon name="wallet" size={14} />
                  本工作区全部已保存订单 · 币种独立核算
                </span>
              </div>
              {g ? (
                <div className="metric-grid">
                  <Metric
                    label="所有状态订单金额"
                    value={money(g.all_status_order_amount)}
                    note={currency + " · 包括未付款与取消等状态"}
                    icon="orders"
                  />
                  <Metric
                    label="已支付且未取消"
                    value={money(g.confirmed_paid_non_cancelled_amount)}
                    note={currency + " · 平台支付金额口径"}
                    icon="wallet"
                  />
                  <Metric
                    label="已知成本订单贡献"
                    value={money(g.estimated_contribution_known_orders)}
                    note={
                      g.cost_complete_orders
                        ? `${currency} · ${g.cost_complete_orders} 笔成本完整订单`
                        : "成本待补充，保持未知"
                    }
                    icon="grid"
                  />
                  <Metric
                    label="已知净结算利润"
                    value={money(g.settled_profit_known_orders)}
                    note={
                      g.settled_known_orders
                        ? `${currency} · ${g.settled_known_orders} 笔已知结算`
                        : "净结算额未核对"
                    }
                    icon="check"
                  />
                </div>
              ) : (
                <EmptyState
                  icon="wallet"
                  title="同步订单后，开始核对财务。"
                  description="订单金额不会直接当作利润，成本未知时保留缺失。"
                  action={
                    <button onClick={() => onNavigate("orders")}>
                      前往订单中心
                      <Icon name="arrow" size={14} />
                    </button>
                  }
                />
              )}
              {g && (
                <article className="cost-coverage">
                  <div className="cost-coverage-icon">
                    <Icon name="wallet" size={23} />
                  </div>
                  <div>
                    <h3>让利润建立在完整成本之上</h3>
                    <p>
                      {financeOrders.filter((o) => o.costs_complete).length} /{" "}
                      {financeOrders.length} 笔 {currency}{" "}
                      订单已确认成本。成本和净结算额目前人工录入。
                    </p>
                    <div className="coverage-track">
                      <span
                        style={{
                          width: `${financeOrders.length ? (financeOrders.filter((o) => o.costs_complete).length / financeOrders.length) * 100 : 0}%`,
                        }}
                      />
                    </div>
                  </div>
                  <button
                    className="secondary"
                    onClick={() =>
                      editFinance(
                        financeOrders.find((o) => !o.costs_complete) ||
                          financeOrders[0],
                      )
                    }
                  >
                    开始核对
                    <Icon name="arrow" size={14} />
                  </button>
                </article>
              )}
              <article className="data-table-panel">
                <div className="table-toolbar">
                  <h2>订单成本与利润</h2>
                  <div className="search-input">
                    <Icon name="search" size={16} />
                    <input
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      aria-label="搜索财务订单"
                      placeholder="搜索订单号"
                    />
                  </div>
                </div>
                {financeOrders.length ? (
                  <>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>订单号</th>
                            <th>店铺</th>
                            <th>成本状态</th>
                            <th className="numeric">订单金额</th>
                            <th className="numeric">贡献估算</th>
                            <th className="numeric">已知净结算利润</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {pageItems(financeOrders).map((o) => (
                            <tr key={o.shop_id + o.order_id}>
                              <td className="order-id">{o.order_id}</td>
                              <td>{shopName(o.shop_id)}</td>
                              <td>
                                <StatusBadge
                                  status={
                                    o.costs_complete
                                      ? "completed"
                                      : "waiting_input"
                                  }
                                  label={
                                    o.costs_complete
                                      ? "成本已核对"
                                      : "待补充成本"
                                  }
                                />
                              </td>
                              <td className="numeric">
                                {money(o.total_amount)}
                              </td>
                              <td className="numeric">
                                {money(o.estimated_contribution)}
                              </td>
                              <td className="numeric">
                                {money(o.settled_profit)}
                              </td>
                              <td>
                                <button
                                  className="text-button"
                                  onClick={() => editFinance(o)}
                                >
                                  核对成本
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {tableFooter(financeOrders.length)}
                  </>
                ) : (
                  <p className="quiet-empty">暂无符合条件的订单</p>
                )}
              </article>
              <p className="data-footnote">
                <Icon name="alert" size={14} />
                金额不跨币种合并。净结算利润不会重复扣减已包含的平台费与退款。
              </p>
            </>
          )}
        </>
      )}
      {drawer && (
        <Drawer
          wide={drawer === "sync"}
          title={
            {
              catalog: "库存档案",
              stock: "录入实物盘点",
              pricing: "人工定价规则",
              finance: "核对订单成本",
              sync: "订单同步管理",
            }[drawer] || "业务设置"
          }
          subtitle={
            {
              catalog: "建立仓库、商品与内部 SKU。",
              stock: "以实际盘点数量维护库存，不读取 TikTok 展示数量。",
              pricing: "按市场保存已确认成本、费率和目标利润率。",
              finance: "未知成本请保留未完整状态，不要用0替代。",
              sync: "读取已授权店铺订单，保留同步范围与完整性。",
            }[drawer]
          }
          onClose={() => setDrawer("")}
        >
          {error && (
            <div className="toast error" role="alert">
              {error.replace(/^Error: /, "")}
            </div>
          )}
          {notice && (
            <div className="toast success" role="status">
              {notice}
            </div>
          )}
          {drawer === "catalog" && (
            <>
              <section className="drawer-section">
                <h3>
                  <span className="step-number">01</span>建立仓库
                </h3>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    act(async () => {
                      await api("/business/warehouses", {
                        name: warehouseName,
                        country,
                        kind: country === "CN" ? "mainland" : "overseas",
                      });
                      setWarehouseName("");
                      setNotice("仓库已创建。");
                    });
                  }}
                >
                  <label>
                    仓库名称
                    <input
                      required
                      value={warehouseName}
                      onChange={(e) => setWarehouseName(e.target.value)}
                      placeholder="例如：深圳仓、吉隆坡仓"
                    />
                  </label>
                  <label>
                    国家 / 地区代码
                    <input
                      required
                      pattern="[A-Z]{2}"
                      maxLength={2}
                      value={country}
                      onChange={(e) => setCountry(e.target.value.toUpperCase())}
                    />
                  </label>
                  <button className="secondary" disabled={busy}>
                    <Icon name="plus" size={14} />
                    添加仓库
                  </button>
                </form>
                <div className="mini-chips">
                  {warehouses.map((w) => (
                    <span className="small-pill" key={w.id}>
                      {w.country} · {w.name}
                    </span>
                  ))}
                </div>
              </section>
              <section className="drawer-section">
                <h3>
                  <span className="step-number">02</span>保存商品事实
                </h3>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    act(async () => {
                      const p = await api("/business/products", {
                        name: productName,
                        facts,
                      });
                      setProductId(p.id);
                      setProductName("");
                      setFacts("");
                      setNotice("商品已保存，继续添加 SKU。");
                    });
                  }}
                >
                  <label>
                    商品名称
                    <input
                      required
                      value={productName}
                      onChange={(e) => setProductName(e.target.value)}
                    />
                  </label>
                  <label>
                    已确认商品事实
                    <textarea
                      value={facts}
                      onChange={(e) => setFacts(e.target.value)}
                      maxLength={8000}
                      placeholder="例如规格、材质与已确认功能，内容 Agent 会据此准备文案。"
                    />
                  </label>
                  <button className="secondary" disabled={busy}>
                    保存商品
                  </button>
                </form>
              </section>
              <section className="drawer-section">
                <h3>
                  <span className="step-number">03</span>添加内部 SKU
                </h3>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    act(async () => {
                      await api("/business/skus", {
                        product_id: productId,
                        code: skuCode,
                      });
                      setSkuCode("");
                      setNotice("SKU 已保存，可以继续录入盘点。");
                    });
                  }}
                >
                  <label>
                    所属商品
                    <select
                      required
                      value={productId}
                      onChange={(e) => setProductId(e.target.value)}
                    >
                      <option value="">选择商品</option>
                      {catalog.products.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    内部 SKU 编码
                    <input
                      required
                      value={skuCode}
                      onChange={(e) => setSkuCode(e.target.value)}
                      placeholder="例如：WATCH-001"
                    />
                  </label>
                  <button disabled={busy}>添加 SKU</button>
                </form>
              </section>
            </>
          )}
          {drawer === "stock" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                act(async () => {
                  const saved = await api("/business/inventory/snapshot", {
                    warehouse_id: stockWarehouse,
                    sku_id: stockSku,
                    on_hand: Number(onHand),
                    warehouse_reserved: Number(reserved),
                    safety_stock: Number(safety),
                    expected_revision: stockRevision,
                    operation_id: crypto.randomUUID(),
                    reason,
                  });
                  setStockRevision(saved.revision);
                  setNotice("盘点已保存。");
                }, true);
              }}
            >
              <label>
                仓库
                <select
                  required
                  value={stockWarehouse}
                  onChange={(e) => editStock(e.target.value, stockSku)}
                >
                  <option value="">选择仓库</option>
                  {warehouses.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name}（{w.country}）
                    </option>
                  ))}
                </select>
              </label>
              <label>
                内部 SKU
                <select
                  required
                  value={stockSku}
                  onChange={(e) => editStock(stockWarehouse, e.target.value)}
                >
                  <option value="">选择 SKU</option>
                  {catalog.skus.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.code}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid">
                {[
                  ["实物数量", onHand, setOnHand],
                  ["仓库已占用", reserved, setReserved],
                  ["安全库存", safety, setSafety],
                ].map(([label, value, set]) => (
                  <label key={String(label)}>
                    {String(label)}
                    <input
                      type="number"
                      min="0"
                      max="100000000"
                      required
                      value={String(value)}
                      onChange={(e) =>
                        (set as (s: string) => void)(e.target.value)
                      }
                    />
                  </label>
                ))}
              </div>
              <label>
                盘点或调整依据
                <textarea
                  required
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  maxLength={500}
                  placeholder="填写盘点日期、确认人或仓库提供的数量依据。"
                />
              </label>
              <p className="field-hint">
                当前编辑依据版本 {stockRevision}
                。仓库占用应包含仓库已确认的订单。
              </p>
              <div className="form-footer">
                <button
                  disabled={busy || !warehouses.length || !catalog.skus.length}
                >
                  确认盘点记录
                </button>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setDrawer("catalog")}
                >
                  先建立档案
                </button>
              </div>
            </form>
          )}
          {drawer === "pricing" && (
            <PricingPanel
              api={api}
              skus={catalog.skus}
              rules={catalog.pricing}
              onSaved={refresh}
            />
          )}
          {drawer === "finance" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!selectedOrder) return;
                act(async () => {
                  const r = await api(
                    `/business/orders/${selectedOrder.shop_id}/${selectedOrder.order_id}/finance`,
                    {
                      ...financeFields,
                      currency: selectedOrder.currency,
                      settled_net: financeFields.settled_net || null,
                      costs_complete: complete,
                      expected_revision: financeRevision,
                    },
                    "PUT",
                  );
                  setFinanceRevision(r.revision);
                  setNotice("成本记录已保存。");
                }, true);
              }}
            >
              <label>
                订单
                <select
                  required
                  value={financeOrder}
                  onChange={(e) => {
                    const o = orders.find(
                      (o) => o.shop_id + ":" + o.order_id === e.target.value,
                    );
                    if (o) editFinance(o);
                  }}
                >
                  <option value="">选择订单</option>
                  {orders.map((o) => (
                    <option
                      key={o.shop_id + o.order_id}
                      value={o.shop_id + ":" + o.order_id}
                    >
                      {o.order_id} · {o.currency}
                    </option>
                  ))}
                </select>
              </label>
              {selectedOrder && (
                <div className="finance-order-summary">
                  <span>{shopName(selectedOrder.shop_id)}</span>
                  <strong>
                    {money(selectedOrder.total_amount)} {selectedOrder.currency}
                  </strong>
                </div>
              )}
              <div className="grid">
                {Object.entries(financeLabels)
                  .filter(([k]) => k !== "source_note")
                  .map(([k, label]) => (
                    <label key={k}>
                      {label}
                      <input
                        inputMode="decimal"
                        required={k !== "settled_net"}
                        value={financeFields[k]}
                        onChange={(e) =>
                          setFinanceFields({
                            ...financeFields,
                            [k]: e.target.value,
                          })
                        }
                      />
                    </label>
                  ))}
              </div>
              <label>
                成本与结算依据
                <textarea
                  required
                  value={financeFields.source_note}
                  onChange={(e) =>
                    setFinanceFields({
                      ...financeFields,
                      source_note: e.target.value,
                    })
                  }
                  placeholder="记录成本来源；有换汇时注明汇率及日期。"
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={complete}
                  onChange={(e) => setComplete(e.target.checked)}
                />
                <span>已核对完整成本，允许计算贡献与利润</span>
              </label>
              <p className="field-hint">
                所有金额按 {selectedOrder?.currency || "订单币种"} 录入。版本{" "}
                {financeRevision}。
              </p>
              <div className="form-footer">
                <button disabled={busy || !selectedOrder}>保存成本记录</button>
              </div>
            </form>
          )}
          {drawer === "sync" && (
            <>
              <section className="drawer-section">
                <h3>按日期同步</h3>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    act(async () => {
                      const d = await api("/business/orders/sync", {
                        shop_id: shopId,
                        start_date: startDate,
                        end_date: endDate,
                      });
                      setNotice(
                        `已读取 ${d.retrieved_count} 笔订单，${d.complete ? "所选范围完整" : "结果未完整，请缩小日期范围"}。`,
                      );
                    });
                  }}
                >
                  <label>
                    已授权店铺
                    <select
                      required
                      value={shopId}
                      onChange={(e) => setShopId(e.target.value)}
                    >
                      <option value="">选择店铺</option>
                      {shops.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}（{s.region}）
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="grid">
                    <label>
                      开始日期
                      <input
                        type="date"
                        required
                        value={startDate}
                        onChange={(e) => setStartDate(e.target.value)}
                      />
                    </label>
                    <label>
                      结束日期
                      <input
                        type="date"
                        required
                        value={endDate}
                        onChange={(e) => setEndDate(e.target.value)}
                      />
                    </label>
                  </div>
                  <button disabled={busy || !shopId}>
                    <Icon name="refresh" size={15} />
                    {busy ? "正在读取…" : "同步所选日期"}
                  </button>
                </form>
                {!shops.length && (
                  <button
                    className="text-button"
                    onClick={() =>
                      act(async () => {
                        const d = await api("/shop/shops");
                        setShops(d.shops);
                        if (d.shops.length) setShopId(d.shops[0].id);
                      })
                    }
                  >
                    重新读取授权店铺
                  </button>
                )}
              </section>
              <section className="drawer-section">
                <h3>定时同步</h3>
                <p className="field-hint">
                  后台运行期间按间隔轮询。更早的订单需要按日期补充同步。
                </p>
                <div className="grid">
                  <label>
                    间隔（秒）
                    <input
                      type="number"
                      min="60"
                      max="86400"
                      value={pollSeconds}
                      onChange={(e) => setPollSeconds(e.target.value)}
                    />
                  </label>
                  <label>
                    回看天数
                    <input
                      type="number"
                      min="1"
                      max="31"
                      value={pollDays}
                      onChange={(e) => setPollDays(e.target.value)}
                    />
                  </label>
                </div>
                <button
                  className="secondary"
                  disabled={busy || !shopId}
                  onClick={() =>
                    act(async () => {
                      const p = polls.find((p) => p.shop_id === shopId),
                        s = shops.find((s) => s.id === shopId),
                        zone =
                          s?.region === "MY"
                            ? "Asia/Kuala_Lumpur"
                            : s?.region === "TH"
                              ? "Asia/Bangkok"
                              : null;
                      if (!zone)
                        throw Error(
                          "当前页面支持 MY / TH 定时同步，其他市场需要明确时区。",
                        );
                      await api(
                        "/business/orders/sync-settings",
                        {
                          shop_id: shopId,
                          enabled: true,
                          interval_seconds: Number(pollSeconds),
                          lookback_days: Number(pollDays),
                          timezone: zone,
                          expected_revision: p?.revision || 0,
                        },
                        "PUT",
                      );
                      setNotice("定时同步设置已保存。");
                    })
                  }
                >
                  开启 / 更新所选店铺
                </button>
                {polls.map((p) => (
                  <div className="sync-setting-row" key={p.id}>
                    <div>
                      <strong>{shopName(p.shop_id)}</strong>
                      <small>
                        {p.enabled
                          ? `每 ${p.interval_seconds / 60} 分钟 · 回看 ${p.lookback_days} 天`
                          : "已停止定时同步"}
                      </small>
                      <small>
                        最后尝试 {formatTime(p.last_attempt_at, true)}
                      </small>
                      {p.last_error && (
                        <small className="danger">{p.last_error}</small>
                      )}
                    </div>
                    <StatusBadge status={p.last_status || "pending"} />
                    <button
                      className="text-button danger"
                      disabled={busy || !p.enabled}
                      onClick={() =>
                        act(async () => {
                          await api(
                            "/business/orders/sync-settings",
                            {
                              shop_id: p.shop_id,
                              enabled: false,
                              interval_seconds: p.interval_seconds,
                              lookback_days: p.lookback_days,
                              timezone: p.timezone,
                              expected_revision: p.revision,
                            },
                            "PUT",
                          );
                        })
                      }
                    >
                      停止
                    </button>
                  </div>
                ))}
              </section>
              <section className="drawer-section">
                <h3>最近同步范围</h3>
                {syncs.length ? (
                  syncs.map((s) => (
                    <div className="sync-setting-row" key={s.id}>
                      <div>
                        <strong>{shopName(s.shop_id)}</strong>
                        <small>
                          {s.window?.start_date} 至 {s.window?.end_date} ·{" "}
                          {s.retrieved_count} 笔
                        </small>
                        <small>{formatTime(s.synced_at)}</small>
                      </div>
                      <StatusBadge
                        status={s.complete ? "completed" : "partial"}
                        label={s.complete ? "范围已读取" : "部分读取"}
                      />
                    </div>
                  ))
                ) : (
                  <p className="field-hint">暂无同步记录</p>
                )}
              </section>
            </>
          )}
        </Drawer>
      )}
    </section>
  );
}
