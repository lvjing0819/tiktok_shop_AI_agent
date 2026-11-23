import { useEffect, useRef, type ReactNode } from "react";
import { MetricHelp } from "./MetricGuide";

export type IconName =
  | "spark"
  | "chat"
  | "grid"
  | "box"
  | "orders"
  | "wallet"
  | "tasks"
  | "search"
  | "arrow"
  | "chevron"
  | "plus"
  | "close"
  | "settings"
  | "refresh"
  | "external"
  | "check"
  | "clock"
  | "alert"
  | "globe"
  | "video"
  | "ads"
  | "logout"
  | "history"
  | "more"
  | "send"
  | "link"
  | "filter"
  | "download";
const paths: Record<IconName, ReactNode> = {
  spark: (
    <>
      <path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3Z" />
      <path d="m20 2 .6 1.4L22 4l-1.4.6L20 6l-.6-1.4L18 4l1.4-.6Z" />
    </>
  ),
  chat: (
    <>
      <path d="M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 0 1-1-4V9a7 7 0 0 1 7-7h3a8 8 0 0 1 8 8Z" />
      <path d="M7 8h9M7 12h6" />
    </>
  ),
  grid: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  box: (
    <>
      <path d="m12 3 9 5-9 5-9-5Z" />
      <path d="M3 8v10l9 5 9-5V8M12 13v10M7 5.8l9 5" />
    </>
  ),
  orders: (
    <>
      <rect x="5" y="3" width="14" height="19" rx="2" />
      <path d="M9 2h6v4H9zM9 11h6M9 15h6M9 19h3" />
    </>
  ),
  wallet: (
    <>
      <path d="M20 8H5a2 2 0 0 1 0-4h13v4M3 6v13a2 2 0 0 0 2 2h15V8" />
      <path d="M20 12h-6v5h6M16 14.5h.1" />
    </>
  ),
  tasks: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="3" />
      <path d="m7 8 1.5 1.5L11 7M14 8h3m-10 8 1.5 1.5L11 15m3 1h3" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 5 5" />
    </>
  ),
  arrow: (
    <>
      <path d="M5 12h14m-5-5 5 5-5 5" />
    </>
  ),
  chevron: <path d="m9 5 7 7-7 7" />,
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  settings: (
    <>
      <path d="m9 3-1 3-3 1-2 3 2 2v3l3 1 1 3h4l1-3 3-1 2-3-2-2V8l-3-1-1-4Z" />
      <circle cx="10.5" cy="11" r="3" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 10a8 8 0 0 0-14-5L3 8m0-5v5h5M4 14a8 8 0 0 0 14 5l3-3m0 5v-5h-5" />
    </>
  ),
  external: (
    <>
      <path d="M14 3h7v7m0-7L10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  alert: (
    <>
      <path d="m12 3 10 18H2Z" />
      <path d="M12 9v5m0 3v.1" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <ellipse cx="12" cy="12" rx="4" ry="9" />
      <path d="M3 12h18M5 7h14M5 17h14" />
    </>
  ),
  video: (
    <>
      <rect x="3" y="5" width="13" height="14" rx="2" />
      <path d="m16 10 5-3v10l-5-3" />
    </>
  ),
  ads: (
    <>
      <path d="m4 10 14-6v16L4 14Zm1 4 2 7h4l-2-5M21 9v6" />
    </>
  ),
  logout: (
    <>
      <path d="M9 3H4v18h5m6-14 5 5-5 5m-6-5h11" />
    </>
  ),
  history: (
    <>
      <path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l4 2" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>
  ),
  send: (
    <>
      <path d="m3 3 18 9-18 9 4-9Z" />
      <path d="M7 12h14" />
    </>
  ),
  link: (
    <>
      <path d="m10 14 4-4M8 15l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" />
    </>
  ),
  filter: (
    <>
      <path d="M4 6h16M7 12h10m-7 6h4" />
    </>
  ),
  download: (
    <>
      <path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" />
    </>
  ),
};
export function Icon({
  name,
  size = 18,
  className = "",
}: {
  name: IconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={"icon " + className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}
export const roleNames: Record<string, string> = {
  research: "选品研究",
  product: "商品运营",
  content: "内容策划",
  ads: "广告分析",
};
export const roleIcons: Record<string, IconName> = {
  research: "search",
  product: "box",
  content: "video",
  ads: "ads",
};
export function RoleMark({ role, size = "" }: { role: string; size?: string }) {
  return (
    <span className={`role-mark ${role} ${size}`}>
      <Icon
        name={roleIcons[role] || "spark"}
        size={size === "large" ? 23 : 17}
      />
    </span>
  );
}
const statusLabels: Record<string, string> = {
  draft: "待开始",
  pending: "待执行",
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  waiting_input: "待补充",
  waiting_approval: "待审批",
  blocked: "待接入",
  failed: "执行失败",
  budget_exceeded: "达到预算",
  rejected: "已驳回",
  awaiting_review: "待审核",
  published: "模拟完成",
  UNPAID: "待付款",
  ON_HOLD: "暂挂",
  AWAITING_SHIPMENT: "待发货",
  PARTIALLY_SHIPPING: "部分发货",
  AWAITING_COLLECTION: "待揽收",
  IN_TRANSIT: "运输中",
  DELIVERED: "已送达",
  COMPLETED: "已完成",
  CANCELLED: "已取消",
  connected: "已连接",
  disconnected: "未连接",
  connecting: "授权中",
  error: "需检查",
  partial: "部分读取",
  local: "本地占用",
  warehouse: "仓库占用",
  shipped: "已出库",
  cancelled: "已取消",
  in_transit: "在途",
  received: "已收货",
};
export function StatusBadge({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const tone = [
    "completed",
    "COMPLETED",
    "DELIVERED",
    "connected",
    "received",
  ].includes(status)
    ? "green"
    : [
          "running",
          "queued",
          "IN_TRANSIT",
          "AWAITING_COLLECTION",
          "connecting",
        ].includes(status)
      ? "blue"
      : ["failed", "error", "budget_exceeded"].includes(status)
        ? "red"
        : [
              "waiting_input",
              "waiting_approval",
              "awaiting_review",
              "blocked",
              "partial",
              "UNPAID",
            ].includes(status)
          ? "amber"
          : "neutral";
  return (
    <span className={`status-badge ${tone}`}>
      <i />
      {label || statusLabels[status] || status}
    </span>
  );
}
export function EmptyState({
  icon = "box",
  title,
  description,
  action,
}: {
  icon?: IconName;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={25} />
      </span>
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}
export function Metric({
  label,
  value,
  note,
  icon,
  tone = "",
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  icon?: IconName;
  tone?: string;
}) {
  return (
    <article className={`metric ${tone}`}>
      <div className="metric-label">
        {label}
        {icon && <Icon name={icon} size={17} />}
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-note">{note || "\u00a0"}</div>
      <MetricHelp label={label}/>
    </article>
  );
}
export function Drawer({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    d?.showModal();
    return () => d?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={"drawer " + (wide ? "wide" : "")}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="drawer-inner">
        <header className="drawer-header">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button
            className="icon-button"
            aria-label="关闭面板"
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </div>
    </dialog>
  );
}
export function formatTime(t?: number | null, short = false) {
  if (!t) return "尚未同步";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(short ? {} : { year: "numeric" as const }),
  }).format(new Date(t * 1000));
}
export function money(
  value: string | number | null | undefined,
  currency?: string,
) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    ...(currency
      ? { style: "currency", currency, currencyDisplay: "narrowSymbol" }
      : {}),
  }).format(n);
}
function inline(text: string): ReactNode[] {
  return text
    .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
    .map((s, i) =>
      s.startsWith("**") ? (
        <strong key={i}>{s.slice(2, -2)}</strong>
      ) : s.startsWith("`") ? (
        <code key={i}>{s.slice(1, -1)}</code>
      ) : (
        s
      ),
    );
}
export function RichText({ text }: { text: string }) {
  const lines = text.split("\n");
  const nodes: ReactNode[] = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.includes("|") && /^\s*\|?\s*:?-{3}/.test(lines[i + 1] || "")) {
      const cells = (s: string) =>
        s
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((c) => c.trim());
      const head = cells(l),
        rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|")) {
        rows.push(cells(lines[i]));
        i++;
      }
      i--;
      nodes.push(
        <div className="table-scroll rich-table" key={i}>
          <table>
            <thead>
              <tr>
                {head.map((h, j) => (
                  <th key={j}>{inline(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, j) => (
                <tr key={j}>
                  {r.map((v, k) => (
                    <td key={k}>{inline(v)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else if (/^#{1,4}\s/.test(l)) {
      nodes.push(<h3 key={i}>{inline(l.replace(/^#+\s/, ""))}</h3>);
    } else if (/^\s*[-*•]\s/.test(l)) {
      nodes.push(
        <div className="rich-bullet" key={i}>
          <span>•</span>
          <div>{inline(l.replace(/^\s*[-*•]\s/, ""))}</div>
        </div>,
      );
    } else if (l.trim()) {
      nodes.push(<p key={i}>{inline(l)}</p>);
    } else nodes.push(<div className="rich-space" key={i} />);
  }
  return <div className="rich-text">{nodes}</div>;
}
