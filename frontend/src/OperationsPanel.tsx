import { useEffect, useState } from "react";
import type { Api, Run } from "./types";
import {
  Icon,
  RoleMark,
  StatusBadge,
  EmptyState,
  Drawer,
  RichText,
  formatTime,
  roleNames,
  money,
} from "./ui";
export default function OperationsPanel({
  api,
  initialRunId,
}: {
  api: Api;
  initialRunId?: string;
}) {
  const [runs, setRuns] = useState<Run[]>([]),
    [active, setActive] = useState<Run | null>(null),
    [creating, setCreating] = useState(false),
    [question, setQuestion] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [filter, setFilter] = useState("all"),
    [search, setSearch] = useState(""),
    [editor, setEditor] = useState(""),
    [traces, setTraces] = useState<any[] | null>(null),
    [tools, setTools] = useState<any[]>([]);
  async function select(id: string) {
    const r = await api("/operations/" + id);
    setActive(r);
    setEditor(JSON.stringify(r.plan, null, 2));
    setTraces(null);
  }
  useEffect(() => {
    let live = true;
    api("/operations")
      .then((r) => {
        if (live) setRuns(r);
      })
      .catch((e) => {
        if (live) setError(String(e));
      });
    api("/operations/tools")
      .then((r) => {
        if (live) setTools(r.tools);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [api]);
  useEffect(() => {
    if (initialRunId) select(initialRunId).catch((e) => setError(String(e)));
  }, [initialRunId, api]);
  useEffect(() => {
    let live = true;
    const timer = setInterval(async () => {
      try {
        const list = await api("/operations");
        if (live) setRuns(list);
        if (active) {
          const r = await api("/operations/" + active.id);
          if (live) setActive(r);
        }
      } catch (e) {
        if (live) setError(String(e));
      }
    }, 4000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api, active?.id]);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      setRuns(await api("/operations"));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const waiting = (r: Run) =>
      [
        "waiting_input",
        "waiting_approval",
        "blocked",
        "failed",
        "budget_exceeded",
      ].includes(r.status),
    running = (r: Run) => ["running", "queued"].includes(r.status);
  const filtered = runs.filter(
    (r) =>
      (filter === "all" ||
        (filter === "active" && running(r)) ||
        (filter === "attention" && waiting(r)) ||
        (filter === "done" && r.status === "completed")) &&
      (!search || r.plan.title.toLowerCase().includes(search.toLowerCase())),
  );
  return (
    <section className="operations-panel">
      <div className="page-heading">
        <div>
          <div className="eyebrow">AGENT OPERATIONS</div>
          <h1>任务中心</h1>
          <p>查看团队正在做什么，确认需要你的决定，检视每一份交付。</p>
        </div>
        <button
          onClick={() => {
            setCreating(true);
            setError("");
          }}
        >
          <Icon name="plus" size={16} />
          新建任务
        </button>
      </div>
      <div className="task-summary-strip">
        <div>
          <span className="summary-dot blue" />
          <strong>{runs.filter(running).length}</strong>
          <span>正在执行</span>
        </div>
        <div>
          <span className="summary-dot amber" />
          <strong>{runs.filter(waiting).length}</strong>
          <span>需要关注</span>
        </div>
        <div>
          <span className="summary-dot green" />
          <strong>{runs.filter((r) => r.status === "completed").length}</strong>
          <span>已完成</span>
        </div>
        <span className="task-summary-note">任务状态自动更新</span>
      </div>
      {error && !active && !creating && (
        <div className="toast error" role="alert">
          {error.replace(/^Error: /, "")}
        </div>
      )}
      <article className="data-table-panel">
        <div className="table-toolbar">
          <div className="table-tabs">
            {[
              ["all", "全部任务"],
              ["active", "执行中"],
              ["attention", "待处理"],
              ["done", "已完成"],
            ].map(([id, label]) => (
              <button
                key={id}
                className={filter === id ? "active" : ""}
                onClick={() => setFilter(id)}
              >
                {label}
                {id === "attention" && runs.filter(waiting).length > 0 && (
                  <span>{runs.filter(waiting).length}</span>
                )}
              </button>
            ))}
          </div>
          <div className="search-input">
            <Icon name="search" size={16} />
            <input
              aria-label="搜索运营任务"
              placeholder="搜索任务"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>
        {filtered.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>任务名称</th>
                  <th>参与团队</th>
                  <th>执行进度</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const states = r.steps || {},
                    done =
                      r.progress?.completed ??
                      (r.status === "completed"
                        ? r.plan.steps.length
                        : Object.values(states).filter(
                            (s) => s.status === "completed",
                          ).length);
                  return (
                    <tr key={r.id}>
                      <td>
                        <button
                          className="table-link"
                          onClick={() => act(() => select(r.id))}
                        >
                          {r.plan.title}
                        </button>
                        <small className="cell-subtitle task-question">
                          {r.question}
                        </small>
                      </td>
                      <td>
                        <div className="role-stack">
                          {Array.from(
                            new Set(r.plan.steps.map((s) => s.agent)),
                          ).map((role) => (
                            <span title={roleNames[role]} key={role}>
                              <RoleMark role={role} />
                            </span>
                          ))}
                        </div>
                      </td>
                      <td>
                        <div className="task-progress">
                          <span>
                            <i
                              style={{
                                width: `${r.plan.steps.length ? (done / r.plan.steps.length) * 100 : 0}%`,
                              }}
                            />
                          </span>
                          <small>
                            {done}/{r.plan.steps.length}
                          </small>
                        </div>
                      </td>
                      <td>
                        <StatusBadge status={r.status} />
                      </td>
                      <td className="muted nowrap">
                        {formatTime(r.created_at, true)}
                      </td>
                      <td>
                        <button
                          className="icon-button"
                          aria-label={"查看任务 " + r.plan.title}
                          onClick={() => act(() => select(r.id))}
                        >
                          <Icon name="arrow" size={16} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon="tasks"
            title={
              runs.length ? "没有符合条件的任务" : "把第一个任务交给运营团队。"
            }
            description="描述目标，Leader 会安排计划与分工。执行状态和交付结果会保留在这里。"
            action={
              <button className="secondary" onClick={() => setCreating(true)}>
                <Icon name="plus" size={15} />
                创建运营任务
              </button>
            }
          />
        )}
        <div className="table-footer">
          <span>共 {filtered.length} 个任务</span>
          <span>执行结果以实际工具记录为准</span>
        </div>
      </article>
      <details className="capability-details">
        <summary>
          <Icon name="link" size={15} />
          查看团队能力与接入状态
        </summary>
        <div className="tool-capability-list">
          {tools.map((t) => (
            <div key={t.name}>
              <span>
                <strong>{t.name}</strong>
                <small>{t.description}</small>
              </span>
              <StatusBadge
                status={t.available ? "completed" : "blocked"}
                label={t.available ? "已接入" : "待接入"}
              />
            </div>
          ))}
        </div>
      </details>
      {creating && (
        <Drawer
          title="创建运营任务"
          subtitle="说清目标、市场与时间范围，Leader 会准备可查看的执行计划。"
          onClose={() => setCreating(false)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              act(async () => {
                const r = await api("/operations/plan", { question });
                setActive(r);
                setEditor(JSON.stringify(r.plan, null, 2));
                setCreating(false);
                setQuestion("");
                setTraces(null);
              });
            }}
          >
            <label>
              你想完成什么？
              <textarea
                className="task-prompt"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                required
                maxLength={4000}
                placeholder="例如：查询 MY 店铺昨天的订单，同时研究女士手表累计销量前3名，并汇总结果。"
              />
            </label>
            <div className="prompt-example">
              <Icon name="spark" />
              <span>
                指定市场、店铺与日期，能让计划更准确。资料不足时会向你确认。
              </span>
            </div>
            {error && (
              <p className="error" role="alert">
                {error.replace(/^Error: /, "")}
              </p>
            )}
            <div className="form-footer">
              <button disabled={busy || !question.trim()}>
                {busy ? "正在规划…" : "生成执行计划"}
                <Icon name="arrow" size={15} />
              </button>
            </div>
          </form>
        </Drawer>
      )}
      {active && !creating && (
        <Drawer
          wide
          title="任务详情"
          subtitle={"创建于 " + formatTime(active.created_at)}
          onClose={() => {
            setActive(null);
            setError("");
          }}
        >
          <div className="task-detail-heading">
            <StatusBadge status={active.status} />
            <span className="muted">计划版本 {active.revision}</span>
            <h2>{active.plan.title}</h2>
            <p>{active.question}</p>
          </div>
          {error && (
            <div className="toast error" role="alert">
              {error.replace(/^Error: /, "")}
            </div>
          )}
          {active.plan.clarification && (
            <div className="inline-notice">
              <Icon name="alert" />
              <div>
                <strong>需要补充信息</strong>
                <p>{active.plan.clarification}</p>
              </div>
            </div>
          )}
          {active.final_delivery && (
            <section className="delivery-report">
              <div className="report-label">
                <Icon name="spark" size={16} />
                Leader 汇总<span>基于团队实际交付</span>
              </div>
              <RichText text={active.final_delivery.answer} />
              {active.final_delivery.limitations.length > 0 && (
                <details>
                  <summary>数据范围与局限</summary>
                  <ul>
                    {active.final_delivery.limitations.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </details>
              )}
            </section>
          )}
          <div className="section-heading">
            <h3>执行计划</h3>
            <span>{active.plan.steps.length} 个步骤</span>
          </div>
          <div className="task-timeline">
            {active.plan.steps.map((s, i) => {
              const state = active.steps?.[s.id],
                result = state?.result;
              return (
                <section className="timeline-step" key={s.id}>
                  <span
                    className={
                      "timeline-number " +
                      (state?.status === "completed" ? "done" : "")
                    }
                  >
                    {state?.status === "completed" ? (
                      <Icon name="check" size={13} />
                    ) : (
                      i + 1
                    )}
                  </span>
                  <div className="timeline-content">
                    <div className="timeline-meta">
                      <RoleMark role={s.agent} />
                      <strong>{roleNames[s.agent]}</strong>
                      <StatusBadge status={state?.status || "pending"} />
                    </div>
                    <h4>{s.goal}</h4>
                    <p className="completion-criteria">{s.success_criteria}</p>
                    {state?.error && <p className="error">{state.error}</p>}
                    {result && (
                      <>
                        <p className="step-conclusion">
                          {result.agent_summary || result.summary}
                        </p>
                        {result.data?.products && (
                          <SelectionProducts products={result.data.products} />
                        )}
                        <details className="step-data">
                          <summary>查看交付数据与来源</summary>
                          {result.data?.rows && (
                            <p>返回 {result.data.rows.length} 条记录</p>
                          )}
                          <pre className="code-block">
                            {JSON.stringify(result.data, null, 2)}
                          </pre>
                          {(result.evidence?.length || 0) > 0 && (
                            <pre className="code-block">
                              {JSON.stringify(result.evidence, null, 2)}
                            </pre>
                          )}
                        </details>
                      </>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
          {active.status === "waiting_approval" && (
            <section className="approval-panel">
              <h3>确认当前版本操作</h3>
              <p>请核对待执行参数。批准仅对本计划版本及冻结参数有效。</p>
              <pre className="code-block">
                {JSON.stringify(active.pending_approval, null, 2)}
              </pre>
              <div className="button-group">
                <button
                  disabled={busy}
                  onClick={() =>
                    act(async () =>
                      setActive(
                        await api("/operations/" + active.id + "/approval", {
                          revision: active.revision,
                          approved: true,
                        }),
                      ),
                    )
                  }
                >
                  <Icon name="check" size={15} />
                  批准本版本
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    act(async () =>
                      setActive(
                        await api("/operations/" + active.id + "/approval", {
                          revision: active.revision,
                          approved: false,
                        }),
                      ),
                    )
                  }
                >
                  驳回
                </button>
              </div>
            </section>
          )}
          {["draft", "failed"].includes(active.status) && (
            <div className="task-action-bar">
              <p>
                {active.status === "draft"
                  ? "确认计划后再开始执行。"
                  : "已完成步骤会保留，未知结果的外部操作不会盲目重试。"}
              </p>
              <button
                disabled={busy}
                onClick={() =>
                  act(async () =>
                    setActive(
                      await api("/operations/" + active.id + "/start", {
                        revision: active.revision,
                      }),
                    ),
                  )
                }
              >
                {active.status === "draft"
                  ? "确认并开始执行"
                  : "重试未完成步骤"}
                <Icon name="arrow" size={15} />
              </button>
            </div>
          )}
          <details>
            <summary>执行用量与记录</summary>
            <div className="execution-stats">
              {[
                ["模型调用", active.usage.model_calls],
                ["业务工具", active.usage.tool_calls],
                ["外部请求", active.usage.remote_calls],
                ["已报告 Token", active.usage.reported_tokens],
              ].map(([label, n]) => (
                <div key={label}>
                  <small>{label}</small>
                  <strong>{Number(n).toLocaleString()}</strong>
                </div>
              ))}
            </div>
            <p className="field-hint">Token 用量不等同于实际账单金额。</p>
            <button
              className="text-button"
              onClick={() =>
                api("/operations/" + active.id + "/traces")
                  .then(setTraces)
                  .catch((e) => setError(String(e)))
              }
            >
              读取工具证据
              <Icon name="arrow" size={14} />
            </button>
            {traces && (
              <pre className="code-block">
                {JSON.stringify(traces, null, 2)}
              </pre>
            )}
          </details>
          <details>
            <summary>高级计划配置</summary>
            <p className="field-hint">
              补充参数或调整计划后会产生新版本，原审批失效。
            </p>
            <textarea
              aria-label="结构化任务计划"
              className="json-editor"
              value={editor}
              onChange={(e) => setEditor(e.target.value)}
            />
            <button
              className="secondary"
              disabled={busy || ["running", "queued"].includes(active.status)}
              onClick={() =>
                act(async () => {
                  const r = await api(
                    "/operations/" + active.id + "/plan",
                    { revision: active.revision, plan: JSON.parse(editor) },
                    "PUT",
                  );
                  setActive(r);
                  setEditor(JSON.stringify(r.plan, null, 2));
                })
              }
            >
              保存新版本
            </button>
          </details>
        </Drawer>
      )}
    </section>
  );
}
function SelectionProducts({ products }: { products: any[] }) {
  if (!products.length) return null;
  return (
    <div className="selection-products">
      {products.slice(0, 10).map((p, i) => {
        const image =
          p.product_image?.thumb_urls?.[0] || p.product_image?.urls?.[0];
        return (
          <div className="selection-product" key={p.id || i}>
            <span className="product-rank">{p.rank || i + 1}</span>
            {image ? (
              <img
                src={image}
                alt=""
                loading="lazy"
                referrerPolicy="no-referrer"
                onError={(e) => {
                  e.currentTarget.style.display = "none";
                }}
              />
            ) : (
              <span className="product-image-placeholder">
                <Icon name="box" size={20} />
              </span>
            )}
            <div>
              <strong>{p.name}</strong>
              <small>{p.shop_name || "店铺未知"}</small>
              <span>
                {p.sales_price
                  ? `${money(p.sales_price.minimum_amount)}${p.sales_price.minimum_amount !== p.sales_price.maximum_amount ? "–" + money(p.sales_price.maximum_amount) : ""} ${p.sales_price.currency}`
                  : "价格未知"}
                <i>·</i>
                {p.cumulative_units_sold != null
                  ? "累计 " +
                    Number(p.cumulative_units_sold).toLocaleString() +
                    " 件"
                  : p.gmv_range
                    ? "GMV " + p.gmv_range
                    : "销量未知"}
              </span>
            </div>
          </div>
        );
      })}
      {products.length > 10 && (
        <p className="field-hint">显示前10条，完整结果见交付数据。</p>
      )}
    </div>
  );
}
