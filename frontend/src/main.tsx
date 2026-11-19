import { useState, useEffect, useCallback } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import AdsPanel from "./AdsPanel";
type Task = {
  id: string;
  status: string;
  version: number;
  product: { name: string; sku: string };
  price?: string;
  copy?: string;
  error?: string;
  events: string[];
};
type Session = {
  id: string;
  title: string;
  summary: string;
  messages?: { role: string; content: string }[];
};
type Preferences = {
  market: string;
  brand_style: string;
  forbidden_claims: string;
  notes: string;
  revision: number;
};
function App() {
  const [shop, setShop] = useState(localStorage.getItem("shop") || "default");
  const [token, setToken] = useState(sessionStorage.getItem("token") || "");
  const [tab, setTab] = useState("chat"),
    [tasks, setTasks] = useState<Task[]>([]),
    [sessions, setSessions] = useState<Session[]>([]),
    [active, setActive] = useState<Session | null>(null);
  const [prefs, setPrefs] = useState<Preferences>({
    market: "",
    brand_style: "",
    forbidden_claims: "",
    notes: "",
    revision: 0,
  });
  const [health, setHealth] = useState<Record<string, unknown>>({}),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [question, setQuestion] = useState("");
  const api = useCallback(
    async (path: string, body?: unknown, method?: string) => {
      const r = await fetch("/api" + path, {
        method: method || (body === undefined ? "GET" : "POST"),
        headers: {
          "Content-Type": "application/json",
          "X-Shop-ID": shop,
          ...(token ? { Authorization: "Bearer " + token } : {}),
        },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      const data = await r.json();
      if (!r.ok)
        throw Error(
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail),
        );
      return data;
    },
    [shop, token],
  );
  async function refresh() {
    const [t, s, h] = await Promise.all([
      api("/tasks"),
      api("/sessions"),
      api("/health"),
    ]);
    setTasks(t);
    setSessions(s);
    setHealth(h);
  }
  useEffect(() => {
    setActive(null);
    setTasks([]);
    setSessions([]);
    setError("");
    api("/memory/preferences")
      .then((p) => setPrefs(p))
      .catch((e) => setError(e.message));
    refresh().catch((e) => setError(e.message));
    const id = setInterval(
      () => refresh().catch((e) => setError(e.message)),
      3000,
    );
    return () => clearInterval(id);
  }, [shop, token]);
  async function act(fn: () => Promise<void>) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const status: Record<string, string> = {
    queued: "等待执行",
    running: "执行中",
    awaiting_review: "待审核",
    queued_review: "等待审核处理",
    failed: "执行失败",
    published: "已模拟上架",
    rejected: "已驳回",
  };
  return (
    <main>
      <header>
        <span>SHOP / AI OPERATIONS</span>
        <h1>你的店铺运营工作台</h1>
        <p>百炼对话 · 可管理的记忆 · 可恢复的任务</p>
      </header>
      <details>
        <summary>店铺与访问设置</summary>
        <label>
          店铺标识（按回车应用）
          <input
            defaultValue={shop}
            disabled={busy}
            pattern="[a-zA-Z0-9_-]{1,64}"
            onKeyDown={(e) => {
              if (e.key === "Enter" && e.currentTarget.checkValidity()) {
                localStorage.setItem("shop", e.currentTarget.value);
                setShop(e.currentTarget.value);
              }
            }}
          />
        </label>
        <label>
          工作台访问令牌（仅本次浏览器会话）
          <input
            type="password"
            defaultValue={token}
            onBlur={(e) => {
              sessionStorage.setItem("token", e.target.value);
              setToken(e.target.value);
            }}
          />
        </label>
      </details>
      <aside>
        当前店铺：{shop} · 模型：
        {health.model_mode === "bailian" ? "百炼" : "离线演示"} · 数据库：
        {String(health.database || "连接中")}。图片、视频和 TikTok
        上架尚为模拟。对话目前提供建议，创建任务请使用商品页。
      </aside>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      <nav>
        {[
          ["chat", "运营对话"],
          ["tasks", "商品任务"],
          ["memory", "店铺记忆"],
          ["ads", "广告分析"],
        ].map(([id, title]) => (
          <button
            className={tab === id ? "selected" : "secondary"}
            key={id}
            onClick={() => setTab(id)}
          >
            {title}
          </button>
        ))}
      </nav>
      {tab === "ads" && (
        <AdsPanel
          key={shop}
          api={api}
          shop={shop}
          modelMode={health.model_mode}
        />
      )}
      {tab === "chat" && (
        <section className="chat-grid">
          <article>
            <h2>会话</h2>
            <button
              disabled={busy}
              onClick={() =>
                act(async () => {
                  setActive(
                    await api("/sessions", {
                      title: "运营会话 " + new Date().toLocaleDateString(),
                    }),
                  );
                })
              }
            >
              新建会话
            </button>
            {sessions.map((s) => (
              <button
                className="session secondary"
                key={s.id}
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    setActive(await api("/sessions/" + s.id));
                  })
                }
              >
                {active?.id === s.id ? "● " : ""}
                {s.title}
              </button>
            ))}
          </article>
          <article>
            <div className="row">
              <h2>{active?.title || "选择或新建会话"}</h2>
              {active && (
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => {
                    if (confirm("删除这段会话、消息和历史摘要？"))
                      act(async () => {
                        await api(
                          "/sessions/" + active.id,
                          undefined,
                          "DELETE",
                        );
                        setActive(null);
                      });
                  }}
                >
                  删除会话
                </button>
              )}
            </div>
            <div className="messages">
              {active?.messages?.map((m, i) => (
                <div className={"message " + m.role} key={i}>
                  <small>{m.role === "user" ? "你" : "运营助手"}</small>
                  <pre>{m.content}</pre>
                </div>
              ))}
            </div>
            {active?.summary && (
              <details>
                <summary>查看当前历史摘要</summary>
                <pre>{active.summary}</pre>
              </details>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!active || !question.trim()) return;
                act(async () => {
                  const s = await api("/sessions/" + active.id + "/messages", {
                    content: question,
                  });
                  setActive(s);
                  setQuestion("");
                });
              }}
            >
              <label>
                你的问题
                <textarea
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="结合我的店铺风格，帮我梳理商品卖点"
                  required
                  maxLength={4000}
                />
              </label>
              <button disabled={busy || !active}>
                {busy ? "处理中…" : "发送"}
              </button>
            </form>
          </article>
        </section>
      )}
      {tab === "memory" && (
        <section className="grid">
          <article>
            <h2>已确认的店铺偏好</h2>
            <p>
              只有你保存的内容会成为长期偏好。更新后用于新对话请求和新商品任务，不会修改既有任务方案。
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                act(async () => {
                  setPrefs(await api("/memory/preferences", prefs, "PUT"));
                  setNotice("店铺记忆已保存");
                });
              }}
            >
              {(
                [
                  ["market", "目标市场"],
                  ["brand_style", "品牌与素材风格"],
                  ["forbidden_claims", "禁用表述"],
                  ["notes", "其他明确要求"],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <textarea
                    value={prefs[key]}
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
              <button disabled={busy}>保存记忆</button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => {
                  if (
                    confirm(
                      "清空当前店铺的长期偏好？历史任务与会话仍保留各自记录。",
                    )
                  )
                    act(async () => {
                      setPrefs(
                        await api("/memory/preferences", undefined, "DELETE"),
                      );
                    });
                }}
              >
                清空偏好
              </button>
            </form>
          </article>
          <article>
            <h2>连接检查</h2>
            <p>百炼密钥由本机服务端 .env 配置，不在此页面输入。</p>
            <button
              disabled={busy}
              onClick={() =>
                act(async () => {
                  const r = await api("/model/check", {});
                  setNotice(
                    r.mode === "demo"
                      ? "当前为离线演示，未调用百炼"
                      : r.response,
                  );
                })
              }
            >
              测试模型连接
            </button>
            <h3>记忆如何使用</h3>
            <p>
              原始消息保存在数据库。模型读取最近 12
              条消息和历史摘要。摘要不是库存、价格或订单的事实来源。
            </p>
            <p>
              删除会话会删除对应消息和摘要。清空店铺偏好不会删除已生成的历史任务；旧任务保留生成时的偏好快照。
            </p>
          </article>
        </section>
      )}
      {tab === "tasks" && (
        <>
          <article>
            <h2>创建商品任务</h2>
            <p>提交后由独立后台进程执行。若一直等待，请确认已启动 worker。</p>
            <form
              className="product-form"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                act(async () => {
                  await api("/tasks", {
                    product: {
                      name: f.get("name"),
                      sku: f.get("sku"),
                      cost: Number(f.get("cost")),
                      stock: Number(f.get("stock")),
                    },
                    target_margin: Number(f.get("margin")) / 100,
                  });
                  setNotice("任务已创建");
                });
              }}
            >
              {[
                ["name", "商品名称", "便携水杯"],
                ["sku", "SKU", "CUP-001"],
                ["cost", "单件成本（美元）", "8"],
                ["stock", "输入库存", "100"],
                ["margin", "目标毛利率 %", "30"],
              ].map(([name, label, value]) => (
                <label key={name}>
                  {label}
                  <input
                    name={name}
                    defaultValue={value}
                    required
                    type={
                      ["cost", "stock", "margin"].includes(name)
                        ? "number"
                        : "text"
                    }
                    step={name === "stock" ? "1" : "any"}
                    min={name === "cost" ? "0.01" : "0"}
                    max={name === "margin" ? "94.9" : undefined}
                  />
                </label>
              ))}
              <button disabled={busy}>准备商品方案</button>
            </form>
          </article>
          <h2>
            商品任务 <small>{tasks.length}</small>
          </h2>
          {tasks.map((t) => (
            <article key={t.id}>
              <div className="row">
                <h3>
                  {t.product.name} / {t.product.sku}
                </h3>
                <span className="badge">{status[t.status] || t.status}</span>
              </div>
              {t.price && <p>示例价格：${t.price}（仅含商品成本）</p>}
              <pre>{t.copy}</pre>
              {t.error && <p className="error">{t.error}</p>}
              <ul>
                {t.events.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
              {t.status === "awaiting_review" && (
                <div className="row">
                  <button
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        await api(`/tasks/${t.id}/review`, {
                          approved: true,
                          version: t.version,
                        });
                      })
                    }
                  >
                    批准并模拟上架
                  </button>
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        await api(`/tasks/${t.id}/review`, {
                          approved: false,
                          version: t.version,
                        });
                      })
                    }
                  >
                    驳回
                  </button>
                </div>
              )}
              {t.status === "failed" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/tasks/${t.id}/retry`, {});
                    })
                  }
                >
                  从检查点重试
                </button>
              )}
            </article>
          ))}
        </>
      )}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
