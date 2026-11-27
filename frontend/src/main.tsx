import {
  useState,
  useEffect,
  useCallback,
  useRef,
  type FormEvent,
} from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import "./metricStyles.css";
import { PageMetricGuide } from "./MetricGuide";
import {
  Icon,
  RoleMark,
  RichText,
  StatusBadge,
  Drawer,
  EmptyState,
  roleNames,
  formatTime,
} from "./ui";
import type { Api, Session, Preferences, Run } from "./types";
import OperationsPanel from "./OperationsPanel";
import BusinessPanel from "./BusinessPanel";
import AdsPanel from "./AdsPanel";
import LegacyPanel from "./LegacyPanel";

const dataViews = ["overview", "orders", "inventory", "finance"];
const pageNames: Record<string, string> = {
  chat: "运营助手",
  operations: "运营任务",
  overview: "经营总览",
  orders: "订单中心",
  inventory: "库存管理",
  finance: "财务分析",
  settings: "连接与设置",
  memory: "工作记忆",
  ads: "广告报告",
  legacy: "历史演示任务",
};
const starts: Record<
  string,
  { title: string; description: string; question: string }[]
> = {
  all: [
    {
      title: "看看广告表现",
      description: "查询消耗、订单与 GMV Max 数据",
      question: "列出已授权的广告账户，我想查看昨天的广告表现。",
    },
    {
      title: "发现值得关注的商品",
      description: "按市场与类目研究真实热销商品",
      question:
        "查询 MY 市场女士手表（类目605254）累计销量最高的前3个联盟商品，保留价格、销量与数据来源。",
    },
    {
      title: "检查库存与经营数据",
      description: "查看实物库存、订单和成本完整性",
      question:
        "查看大陆仓和海外仓实物库存，再检查当前订单与财务数据的完整性。",
    },
  ],
  research: [
    {
      title: "女士手表选品",
      description: "MY 市场 · 联盟累计销量",
      question:
        "查询 MY 市场女士手表（类目605254）累计销量最高的前3个联盟商品，保留价格、销量与来源。",
    },
    {
      title: "研究热销榜",
      description: "按市场查询近7天 GMV 区间",
      question:
        "查询 TH 市场女士手表近7天的 GMV 榜单候选，说明榜单范围与缺失数据。",
    },
  ],
  product: [
    {
      title: "检查实物库存",
      description: "大陆仓和海外仓独立记录",
      question: "检查大陆仓和海外仓的实物库存，指出未知库存和需要复核的记录。",
    },
    {
      title: "准备商品草案",
      description: "先检查商品档案与定价资料",
      question:
        "查看内部商品档案、SKU和实际库存，告诉我准备上架草案还缺哪些资料。先不要发布。",
    },
  ],
  content: [
    {
      title: "准备商品内容",
      description: "从已确认的商品事实开始",
      question:
        "查看内部商品档案，准备 MY 市场商品文案和视频脚本前，还需要确认哪些资料？",
    },
    {
      title: "分析内容账号",
      description: "查询已授权账号与作品",
      question:
        "查询已授权的 TikTok 内容账号，查看目前可获取的账号与作品数据。",
    },
  ],
  ads: [
    {
      title: "昨天的广告表现",
      description: "先发现真实授权广告账户",
      question: "列出已授权的广告账户，我想查看昨天的广告表现。",
    },
    {
      title: "结合库存分析投放",
      description: "先检查实物库存与数据条件",
      question:
        "先查看实物库存与经营数据完整性，再告诉我分析广告投放还需要选择哪些账户和日期。不要修改广告。",
    },
  ],
};
function initialView() {
  const view = new URLSearchParams(location.search).get("view") || "chat";
  return pageNames[view] ? view : "chat";
}
function App() {
  const [shop, setShop] = useState(localStorage.getItem("shop") || "default"),
    [token, setToken] = useState(sessionStorage.getItem("token") || "");
  const [view, setView] = useState(initialView),
    [mode, setMode] = useState(
      dataViews.includes(initialView()) ? "data" : "assistant",
    ),
    [role, setRole] = useState("all"),
    [mobileNav, setMobileNav] = useState(false);
  const [health, setHealth] = useState<Record<string, any>>({}),
    [sessions, setSessions] = useState<Session[]>([]),
    [active, setActive] = useState<Session | null>(null),
    [runs, setRuns] = useState<Run[]>([]),
    [question, setQuestion] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [access, setAccess] = useState(false),
    [openedRun, setOpenedRun] = useState<string | undefined>();
  const [prefs, setPrefs] = useState<Preferences>({
    market: "",
    brand_style: "",
    forbidden_claims: "",
    notes: "",
    revision: 0,
  });
  const messagesEnd = useRef<HTMLDivElement>(null),
    composer = useRef<HTMLTextAreaElement>(null),
    requestVersion = useRef(0);
  const api: Api = useCallback(
    async (path, body, method, responseType = "json") => {
      const r = await fetch("/api" + path, {
        method: method || (body === undefined ? "GET" : "POST"),
        headers: {
          "Content-Type": "application/json",
          "X-Shop-ID": shop,
          ...(token ? { Authorization: "Bearer " + token } : {}),
        },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      if (r.ok && responseType === "blob") return await r.blob();
      const data = await r.json();
      if (!r.ok) {
        if (r.status === 401) setAccess(true);
        throw Error(
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail),
        );
      }
      return data;
    },
    [shop, token],
  );
  function navigate(next: string, runId?: string) {
    if (next === "chat") {
      location.assign("/analytics.html?view=hub");
      return;
    }
    if (next === "overview") {
      location.assign("/analytics.html");
      return;
    }
    setView(next);
    if (dataViews.includes(next)) setMode("data");
    else if (next === "chat" || next === "operations") setMode("assistant");
    setMobileNav(false);
    setOpenedRun(runId);
    const u = new URL(location.href);
    u.searchParams.set("view", next);
    history.replaceState(null, "", u);
    setError("");
    setNotice("");
  }
  useEffect(() => {
    let live = true;
    requestVersion.current++;
    setActive(null);
    setSessions([]);
    setRuns([]);
    setError("");
    async function load() {
      try {
        const [h, s, r] = await Promise.all([
          api("/health"),
          api("/sessions"),
          api("/operations"),
        ]);
        if (live) {
          setHealth(h);
          setSessions(s);
          setRuns(r);
        }
      } catch (e) {
        if (live) setError(String(e));
      }
    }
    load();
    api("/memory/preferences")
      .then((p) => {
        if (live) setPrefs(p);
      })
      .catch(() => {});
    const timer = setInterval(load, 12000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api]);
  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [active?.messages?.length, busy]);
  async function action(fn: () => Promise<void>) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function send(e?: FormEvent) {
    e?.preventDefault();
    if (busy || !question.trim()) return;
    const content = question.trim(),
      version = requestVersion.current;
    setQuestion("");
    setBusy(true);
    setError("");
    try {
      let session = active;
      if (!session) {
        session = await api("/sessions", { title: content.slice(0, 32) });
        if (version !== requestVersion.current) return;
        setActive(session);
      }
      setActive({
        ...session!,
        messages: [...(session!.messages || []), { role: "user", content }],
      });
      const updated = await api("/sessions/" + session!.id + "/messages", {
        content,
      });
      if (version !== requestVersion.current) return;
      setActive(updated);
      setSessions(await api("/sessions"));
      setRuns(await api("/operations"));
    } catch (e) {
      if (version === requestVersion.current) {
        setError(String(e));
        setQuestion(content);
        if (active)
          setActive(await api("/sessions/" + active.id).catch(() => active));
        else setActive(null);
      }
    } finally {
      if (version === requestVersion.current) setBusy(false);
    }
  }
  const pending = runs.filter((r) =>
    ["waiting_input", "waiting_approval", "failed", "blocked"].includes(
      r.status,
    ),
  ).length;
  const activeRuns = runs.filter((r) =>
    ["queued", "running"].includes(r.status),
  );
  function chooseRole(next: string) {
    setRole(next);
    setActive(null);
    setQuestion("");
    navigate("chat");
  }
  const mainNav =
    mode === "assistant"
      ? [
          { id: "chat", title: "运营对话", icon: "chat" as const },
          { id: "operations", title: "任务中心", icon: "tasks" as const },
        ]
      : [
          { id: "overview", title: "经营总览", icon: "grid" as const },
          { id: "orders", title: "订单中心", icon: "orders" as const },
          { id: "inventory", title: "库存管理", icon: "box" as const },
          { id: "finance", title: "财务分析", icon: "wallet" as const },
        ];
  return (
    <div className={`app-shell ${mobileNav ? "nav-open" : ""}`}>
      {mobileNav && (
        <button
          className="nav-scrim"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        />
      )}
      <aside className="sidebar">
        <a
          className="brand"
          href="?view=chat"
          onClick={(e) => {
            e.preventDefault();
            navigate("chat");
          }}
        >
          <span className="brand-symbol">
            <Icon name="box" size={23} />
          </span>
          <div>
            <strong>
              SHOP OS<span className="brand-period">.</span>
            </strong>
            <small>COMMERCE WORKSPACE</small>
          </div>
        </a>
        <div className="workspace-switch" role="group" aria-label="切换工作区">
          <button
            className={mode === "assistant" ? "active" : ""}
            onClick={() => navigate("chat")}
          >
            <Icon name="spark" size={16} />
            AI 运营助手
          </button>
          <button
            className={mode === "data" ? "active" : ""}
            onClick={() => navigate("overview")}
          >
            <Icon name="grid" size={16} />
            经营看板
          </button>
        </div>
        <div className="nav-section-label">
          {mode === "assistant" ? "工作空间" : "经营数据"}
        </div>
        <nav className="main-nav" aria-label="主导航">
          {mainNav.map((n) => (
            <button
              className={"nav-item " + (view === n.id ? "active" : "")}
              aria-current={view === n.id ? "page" : undefined}
              key={n.id}
              onClick={() => {
                if (n.id === "chat") setRole("all");
                navigate(n.id);
              }}
            >
              <Icon name={n.icon} />
              <span>{n.title}</span>
              {n.id === "operations" && pending > 0 && <em>{pending}</em>}
            </button>
          ))}
        </nav>
        {mode === "assistant" && (
          <>
            <div className="nav-section-label team-label">
              业务智能体<span>4</span>
            </div>
            <nav className="agent-nav" aria-label="业务智能体">
              {Object.entries(roleNames).map(([id, name]) => (
                <button
                  key={id}
                  className={
                    "agent-nav-item " +
                    (view === "chat" && role === id ? "active" : "")
                  }
                  disabled={busy}
                  onClick={() => chooseRole(id)}
                >
                  <RoleMark role={id} />
                  <span>{name}</span>
                  <span
                    className={
                      "agent-dot " +
                      (activeRuns.some((r) =>
                        r.plan.steps.some((s) => s.agent === id),
                      )
                        ? "working"
                        : "")
                    }
                  />
                </button>
              ))}
            </nav>
          </>
        )}
        <div className="sidebar-spacer" />
        <nav className="secondary-nav" aria-label="工作区设置">
          <button
            className={"nav-item " + (view === "memory" ? "active" : "")}
            onClick={() => navigate("memory")}
          >
            <Icon name="history" />
            <span>工作记忆</span>
          </button>
          <button
            className={"nav-item " + (view === "settings" ? "active" : "")}
            onClick={() => navigate("settings")}
          >
            <Icon name="settings" />
            <span>连接与设置</span>
          </button>
        </nav>
        <button className="workspace-profile" onClick={() => setAccess(true)}>
          <span className="workspace-avatar">OP</span>
          <span>
            <strong>内部运营空间</strong>
            <small>{shop === "default" ? "默认工作区" : shop}</small>
          </span>
          <Icon name="more" size={17} />
        </button>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMobileNav(!mobileNav)}
            >
              <Icon name="grid" />
            </button>
            <span className="breadcrumb-root">
              {mode === "assistant" ? "AI 工作台" : "数据工作台"}
            </span>
            <Icon name="chevron" size={12} />
            <strong>{pageNames[view]}</strong>
            {view === "chat" && role !== "all" && (
              <span className="small-pill">{roleNames[role]}</span>
            )}
          </div>
          <div className="topbar-right">
            <span className="model-indicator">
              <i className={health.model_mode === "bailian" ? "online" : ""} />
              {health.model_mode === "bailian"
                ? "百炼模式"
                : health.model_mode
                  ? "离线演示"
                  : "连接中"}
            </span>
            <span className="topbar-divider" />
            <button
              className="icon-button"
              aria-label="打开连接设置"
              onClick={() => navigate("settings")}
            >
              <Icon name="settings" size={18} />
            </button>
            <button
              className="avatar-button"
              aria-label="工作区访问设置"
              onClick={() => setAccess(true)}
            >
              OP
            </button>
          </div>
        </header>
        <main
          className={"page-content " + (view === "chat" ? "chat-page" : "")}
          key={shop + token}
        >
          {error && (
            <div className="toast error" role="alert">
              <Icon name="alert" />
              <span>{error.replace(/^Error: /, "")}</span>
              <button
                className="icon-button"
                aria-label="关闭提示"
                onClick={() => setError("")}
              >
                <Icon name="close" size={15} />
              </button>
            </div>
          )}
          {notice && (
            <div className="toast success" role="status">
              <Icon name="check" />
              {notice}
            </div>
          )}
          {view === "chat" && (
            <div className="chat-workspace">
              <section className="chat-main">
                <div className="chat-toolbar">
                  <div className="chat-thread-title">
                    <Icon name="chat" size={16} />
                    <span>{active?.title || "新的运营对话"}</span>
                    {active && <span className="small-pill">已保存</span>}
                  </div>
                  <div className="button-group">
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() => {
                        setActive(null);
                        setQuestion("");
                        composer.current?.focus();
                      }}
                    >
                      <Icon name="plus" size={15} />
                      新对话
                    </button>
                    <details className="history-menu">
                      <summary className="icon-button" aria-label="会话记录">
                        <Icon name="history" size={18} />
                      </summary>
                      <div className="history-popover">
                        <h4>最近对话</h4>
                        {sessions.length === 0 ? (
                          <p>还没有对话记录</p>
                        ) : (
                          sessions.slice(0, 20).map((s) => (
                            <button
                              className={active?.id === s.id ? "active" : ""}
                              key={s.id}
                              disabled={busy}
                              onClick={() =>
                                action(async () => {
                                  setActive(await api("/sessions/" + s.id));
                                  document
                                    .querySelector(".history-menu")
                                    ?.removeAttribute("open");
                                })
                              }
                            >
                              <Icon name="chat" size={14} />
                              <span>{s.title}</span>
                            </button>
                          ))
                        )}
                      </div>
                    </details>
                    {active && (
                      <button
                        className="icon-button"
                        title="删除当前会话"
                        aria-label="删除当前会话"
                        disabled={busy}
                        onClick={() => {
                          if (confirm("删除当前会话及消息记录？"))
                            action(async () => {
                              await api(
                                "/sessions/" + active.id,
                                undefined,
                                "DELETE",
                              );
                              setActive(null);
                              setSessions(await api("/sessions"));
                            });
                        }}
                      >
                        <Icon name="close" size={16} />
                      </button>
                    )}
                  </div>
                </div>
                <div
                  className={
                    "conversation-scroll " +
                    (!active?.messages?.length ? "is-empty" : "")
                  }
                >
                  {!active?.messages?.length ? (
                    <div className="chat-welcome">
                      <div className="welcome-eyebrow">
                        <span className="live-dot" />
                        YOUR COMMERCE COPILOT
                      </div>
                      <div className="welcome-mark">
                        {role === "all" ? (
                          <Icon name="spark" size={34} />
                        ) : (
                          <RoleMark role={role} size="large" />
                        )}
                      </div>
                      <h1>
                        {role === "all" ? (
                          <>
                            把运营问题，
                            <span className="hero-break">
                              交给你的 AI 团队。
                            </span>
                          </>
                        ) : (
                          `${roleNames[role]}，从一个问题开始。`
                        )}
                      </h1>
                      <p>
                        从数据查询到运营决策，在一次对话中连接你的店铺、内容与广告。
                      </p>
                      <div className="suggestion-grid">
                        {(starts[role] || starts.all).map((s, i) => (
                          <button
                            className="suggestion-card"
                            key={s.title}
                            onClick={() => {
                              setQuestion(s.question);
                              composer.current?.focus();
                            }}
                          >
                            <span className={"suggestion-icon suggestion-" + i}>
                              <Icon
                                name={
                                  i === 0
                                    ? role === "all"
                                      ? "ads"
                                      : "search"
                                    : i === 1
                                      ? "box"
                                      : "grid"
                                }
                                size={20}
                              />
                            </span>
                            <strong>{s.title}</strong>
                            <small>{s.description}</small>
                            <Icon
                              name="arrow"
                              className="suggestion-arrow"
                              size={16}
                            />
                          </button>
                        ))}
                      </div>
                      <div className="chat-capabilities">
                        <span>已接入能力</span>
                        <span>
                          <Icon name="box" size={13} />
                          TikTok Shop 查询
                        </span>
                        <span>
                          <Icon name="ads" size={13} />
                          Ads 分析
                        </span>
                        <span>
                          <Icon name="video" size={13} />
                          Social 查询
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className="chat-transcript">
                      {active.messages.map((m, i) => (
                        <div className={"chat-message " + m.role} key={i}>
                          {m.role !== "user" && (
                            <span className="assistant-avatar">
                              <Icon name="spark" size={18} />
                            </span>
                          )}
                          <div className="chat-message-body">
                            {m.role !== "user" && (
                              <div className="message-meta">
                                运营助手<span>SHOP OS</span>
                              </div>
                            )}
                            <RichText text={m.content} />
                          </div>
                        </div>
                      ))}
                      {busy && (
                        <div className="chat-message assistant">
                          <span className="assistant-avatar">
                            <Icon name="spark" size={18} />
                          </span>
                          <div className="thinking-indicator">
                            <span />
                            <span />
                            <span />
                            <small>正在处理你的问题</small>
                          </div>
                        </div>
                      )}
                      <div ref={messagesEnd} />
                    </div>
                  )}
                </div>
                <div className="composer-area">
                  <form className="chat-composer" onSubmit={send}>
                    <textarea
                      ref={composer}
                      aria-label="给运营助手发送任务"
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      placeholder={
                        role === "all"
                          ? "问问广告表现、发现热销商品，或者交给我一个运营任务…"
                          : `告诉${roleNames[role]}你想解决的问题…`
                      }
                      maxLength={4000}
                      rows={2}
                      onKeyDown={(e) => {
                        if (
                          e.key === "Enter" &&
                          !e.shiftKey &&
                          !e.nativeEvent.isComposing
                        ) {
                          e.preventDefault();
                          send();
                        }
                      }}
                    />
                    <div className="composer-bottom">
                      <span className="composer-context">
                        <Icon name="globe" size={14} />
                        {prefs.market || "已授权业务数据"}
                        <span>·</span>自动分配智能体
                      </span>
                      <button
                        className="send-button"
                        type="submit"
                        aria-label="发送消息"
                        disabled={busy || !question.trim()}
                      >
                        <Icon name={busy ? "clock" : "arrow"} size={19} />
                      </button>
                    </div>
                  </form>
                  <div className="composer-footnote">
                    <span>Enter 发送 · Shift + Enter 换行</span>
                    <span>查询结果以工具来源为准，任务创建后可追踪进度。</span>
                  </div>
                </div>
              </section>
              <aside className="assistant-context">
                <div className="context-header">
                  <span>运营协作</span>
                  <Icon name="spark" size={15} />
                </div>
                <div className="leader-card">
                  <span className="leader-avatar">
                    <Icon name="spark" size={20} />
                  </span>
                  <div>
                    <strong>运营 Leader</strong>
                    <p>理解目标 · 分派任务 · 汇总结论</p>
                  </div>
                  <span className="small-pill">协调者</span>
                </div>
                <div className="context-section-title">你的业务团队</div>
                {Object.entries(roleNames).map(([id, name]) => (
                  <button
                    className="team-row"
                    key={id}
                    disabled={busy}
                    onClick={() => chooseRole(id)}
                  >
                    <RoleMark role={id} />
                    <span>
                      <strong>{name}</strong>
                      <small>
                        {
                          {
                            research: "市场榜单与选品依据",
                            product: "商品、实物库存与上架草案",
                            content: "商品文案与素材脚本",
                            ads: "投放数据与表现分析",
                          }[id]
                        }
                      </small>
                    </span>
                    <Icon name="chevron" size={12} />
                  </button>
                ))}
                <div className="context-section-title">
                  最近任务
                  <button
                    className="text-button"
                    onClick={() => navigate("operations")}
                  >
                    全部
                    <Icon name="arrow" size={13} />
                  </button>
                </div>
                {runs.length === 0 ? (
                  <p className="context-empty">
                    复合任务创建后，进度会出现在这里。
                  </p>
                ) : (
                  runs.slice(0, 3).map((r) => (
                    <button
                      key={r.id}
                      className="context-task"
                      onClick={() => navigate("operations", r.id)}
                    >
                      <span>{r.plan.title}</span>
                      <StatusBadge status={r.status} />
                    </button>
                  ))
                )}
                <div className="context-footer">
                  <Icon name="link" size={15} />
                  <span>连接真实数据，保留每一步来源。</span>
                </div>
              </aside>
            </div>
          )}
          {view === "operations" && (
            <OperationsPanel api={api} initialRunId={openedRun} />
          )}
          {dataViews.includes(view) && (
            <BusinessPanel
              api={api}
              initialTab={view}
              runs={runs}
              onNavigate={navigate}
            />
          )}
          {view === "memory" && (
            <>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">WORKSPACE MEMORY</div>
                  <h1>工作记忆</h1>
                  <p>让每一次新对话，都了解你的市场、风格与运营要求。</p>
                </div>
                <span className="small-pill">
                  人工确认 · 版本 {prefs.revision}
                </span>
              </div>
              <div className="settings-grid">
                <article>
                  <div className="panel-heading">
                    <h2>运营偏好</h2>
                    <Icon name="history" />
                  </div>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      action(async () => {
                        setPrefs(
                          await api("/memory/preferences", prefs, "PUT"),
                        );
                        setNotice("工作记忆已保存，将用于后续新请求。");
                      });
                    }}
                  >
                    {(
                      [
                        ["market", "目标市场", "例如：MY、TH"],
                        [
                          "brand_style",
                          "品牌与内容风格",
                          "描述品牌语气、视觉风格与目标受众",
                        ],
                        [
                          "forbidden_claims",
                          "禁用表述",
                          "列出未经证实的功效、认证或其他禁用说法",
                        ],
                        ["notes", "其他运营要求", "补充团队需要始终遵守的要求"],
                      ] as const
                    ).map(([key, label, placeholder]) => (
                      <label key={key}>
                        {label}
                        <textarea
                          value={prefs[key]}
                          placeholder={placeholder}
                          onChange={(e) =>
                            setPrefs({ ...prefs, [key]: e.target.value })
                          }
                          maxLength={
                            key === "market"
                              ? 100
                              : key === "brand_style"
                                ? 1000
                                : 2000
                          }
                        />
                      </label>
                    ))}
                    <div className="form-footer">
                      <button disabled={busy}>保存记忆</button>
                      <button
                        type="button"
                        className="text-button danger"
                        disabled={busy}
                        onClick={() => {
                          if (
                            confirm(
                              "清空长期运营偏好？历史消息和任务仍会保留。",
                            )
                          )
                            action(async () => {
                              setPrefs(
                                await api(
                                  "/memory/preferences",
                                  undefined,
                                  "DELETE",
                                ),
                              );
                            });
                        }}
                      >
                        清空偏好
                      </button>
                    </div>
                  </form>
                </article>
                <article className="memory-explainer">
                  <span className="empty-icon">
                    <Icon name="history" size={23} />
                  </span>
                  <h2>记住偏好，事实实时查询。</h2>
                  <p>
                    只有你确认保存的内容会成为长期偏好。订单、库存与价格始终来自业务数据源。
                  </p>
                  <div className="explain-row">
                    <Icon name="check" />
                    <span>完整消息保留在会话中</span>
                  </div>
                  <div className="explain-row">
                    <Icon name="check" />
                    <span>近期对话与历史摘要参与上下文</span>
                  </div>
                  <div className="explain-row">
                    <Icon name="check" />
                    <span>更新偏好不改写历史任务方案</span>
                  </div>
                </article>
              </div>
            </>
          )}
          {view === "settings" && (
            <>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">WORKSPACE SETTINGS</div>
                  <h1>连接与设置</h1>
                  <p>管理当前工作区的数据连接与访问方式。</p>
                </div>
                <button className="secondary" onClick={() => setAccess(true)}>
                  <Icon name="settings" />
                  访问设置
                </button>
              </div>
              <div className="settings-grid">
                <article>
                  <div className="panel-heading">
                    <h2>模型服务</h2>
                    <StatusBadge
                      status={
                        health.key_configured && health.model_mode === "bailian"
                          ? "connected"
                          : "disconnected"
                      }
                      label={
                        health.key_configured && health.model_mode === "bailian"
                          ? "已配置"
                          : "待配置"
                      }
                    />
                  </div>
                  <div className="integration-brand">
                    <span className="integration-logo bailian">Q</span>
                    <div>
                      <strong>阿里云百炼</strong>
                      <p>用于对话路由、任务规划与内容分析</p>
                    </div>
                  </div>
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      action(async () => {
                        const r = await api("/model/check", {});
                        setNotice(r.response);
                      })
                    }
                  >
                    <Icon name="refresh" size={15} />
                    测试连接
                  </button>
                  <p className="field-hint">密钥保存在本机服务端配置中。</p>
                </article>
                <Connections api={api} onAds={() => navigate("ads")} />
              </div>
              <article className="capability-summary">
                <div className="panel-heading">
                  <h2>执行能力</h2>
                  <span className="muted">按实际接入状态展示</span>
                </div>
                <div className="capability-grid">
                  {[
                    ["真实业务查询", "已接入", "green"],
                    ["文案与上架草案", "可准备", "blue"],
                    ["图片与视频生成", "待接入", "neutral"],
                    ["真实商品发布", "待接入", "neutral"],
                    ["仓库 API", "待提供", "neutral"],
                    ["自动财务结算", "待接入", "neutral"],
                  ].map(([name, label, tone]) => (
                    <div key={name}>
                      <span>{name}</span>
                      <span className={"status-badge " + tone}>{label}</span>
                    </div>
                  ))}
                </div>
              </article>
              <button
                className="text-button muted"
                onClick={() => navigate("legacy")}
              >
                查看历史模拟商品任务
                <Icon name="arrow" size={14} />
              </button>
            </>
          )}
          {view === "ads" && (
            <AdsPanel api={api} shop={shop} modelMode={health.model_mode} />
          )}
          {view === "legacy" && <LegacyPanel api={api} />}
        <PageMetricGuide page={view === "chat" ? "chat" : view === "operations" ? "operations" : view === "legacy" ? "legacy" : view === "ads" ? "ads" : view === "finance" ? "finance" : view === "inventory" ? "inventory" : view === "orders" ? "orders" : view === "overview" ? "overview" : "chat"}/>
        </main>
        <footer className="app-footer">
          <span>
            SHOP OS <span className="muted">/</span> 内部运营工作区
          </span>
          <span>
            {mode === "assistant"
              ? "智能协作，决策有据。"
              : "已保存数据快照 · 库存以实物账为准"}
          </span>
        </footer>
      </div>
      {access && (
        <Drawer
          title="工作区访问设置"
          subtitle="设置当前业务空间和本次浏览器会话的访问令牌。"
          onClose={() => setAccess(false)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget),
                s = String(f.get("shop") || "default"),
                t = String(f.get("token") || "");
              localStorage.setItem("shop", s);
              sessionStorage.setItem("token", t);
              setShop(s);
              setToken(t);
              setAccess(false);
              setBusy(false);
            }}
          >
            <label>
              工作区标识
              <input
                name="shop"
                required
                pattern="[a-zA-Z0-9_-]{1,64}"
                maxLength={64}
                defaultValue={shop}
              />
            </label>
            <label>
              访问令牌
              <input
                name="token"
                type="password"
                autoComplete="off"
                defaultValue={token}
                placeholder="仅在服务端启用访问保护时需要"
              />
            </label>
            <p className="field-hint">
              工作区用于隔离业务记录，区别于 TikTok 平台店铺 ID。
            </p>
            <div className="form-footer">
              <button type="submit">保存并进入</button>
            </div>
          </form>
        </Drawer>
      )}
    </div>
  );
}
function Connections({ api, onAds }: { api: Api; onAds: () => void }) {
  const [connections, setConnections] = useState<Record<string, any>>({});
  useEffect(() => {
    let live = true;
    Promise.allSettled(
      ["shop", "ads", "social"].map(async (k) => [
        k,
        await api("/" + k + "/connection"),
      ]),
    ).then((rs) => {
      if (live)
        setConnections(
          Object.fromEntries(
            rs
              .filter((r) => r.status === "fulfilled")
              .map((r) => (r as PromiseFulfilledResult<any>).value),
          ),
        );
    });
    return () => {
      live = false;
    };
  }, [api]);
  return (
    <article>
      <div className="panel-heading">
        <h2>业务数据源</h2>
        <Icon name="link" />
      </div>
      {[
        ["shop", "TikTok Shop", "授权店铺、商品与订单"],
        ["ads", "TikTok Ads", "广告账户与投放报表"],
        ["social", "TikTok Social", "账号、作品与评论"],
      ].map(([key, name, note]) => (
        <div className="integration-row" key={key}>
          <span className={"integration-logo " + key}>
            <Icon
              name={key === "shop" ? "box" : key === "ads" ? "ads" : "video"}
              size={21}
            />
          </span>
          <span>
            <strong>{name}</strong>
            <small>{note}</small>
          </span>
          <StatusBadge
            status={connections[key]?.status || "disconnected"}
            label={!connections[key] ? "待检查" : undefined}
          />
        </div>
      ))}
      <button className="text-button" onClick={onAds}>
        管理广告授权与历史报告
        <Icon name="arrow" size={14} />
      </button>
      <p className="field-hint">
        已授权记录不代表每次查询都能成功，具体以接口结果为准。
      </p>
    </article>
  );
}
if (
  !new URLSearchParams(location.search).get("view") ||
  new URLSearchParams(location.search).get("view") === "chat"
) {
  location.replace("/analytics.html?view=hub");
} else if (new URLSearchParams(location.search).get("view") === "overview") {
  location.replace("/analytics.html?view=dashboard");
} else {
  createRoot(document.getElementById("root")!).render(<App />);
}
