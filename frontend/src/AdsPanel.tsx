import { MetricHelp, MetricScope, DeltaPair } from "./MetricGuide";
import { useEffect, useState } from "react";

type Api = (path: string, body?: unknown, method?: string) => Promise<any>;
type Connection = { status: string; error?: string; read_only?: boolean };
type Tool = {
  name: string;
  description: string;
  allowed: boolean;
  input_schema: unknown;
};
type Report = {
  id: string;
  advertiser_id: string;
  question: string;
  start_date: string;
  end_date: string;
  retention_revision?: number;
  pinned?: boolean;
  expired?: boolean;
  created_at?: number;
  answer?: string;
  evidence?: unknown[];
  limitations?: string[];
  tool_calls: number;
  comparison_windows?: any;
  metric_comparisons?: any[];
};
function localDate(offset: number) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function AdsPanel({
  api,
  shop,
  modelMode,
}: {
  api: Api;
  shop: string;
  modelMode: unknown;
}) {
  const [connection, setConnection] = useState<Connection>({
    status: "loading",
  });
  const [attempt, setAttempt] = useState(
    sessionStorage.getItem("ads-attempt:" + shop) || "",
  );
  const [authUrl, setAuthUrl] = useState("");
  const [tools, setTools] = useState<Tool[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const labels: Record<string, string> = {
    loading: "读取连接状态",
    disconnected: "未连接",
    connecting: "等待授权",
    connected: "已连接",
    error: "连接未完成",
  };
  useEffect(() => {
    let live = true;
    async function poll() {
      try {
        const c = await api("/ads/connection");
        if (!live) return;
        setConnection(c);
        if (attempt && c.status === "connecting") {
          const a = await api("/ads/attempts/" + attempt);
          if (live) setAuthUrl(a.authorization_url || "");
        }
      } catch (e) {
        if (live) setError(String(e));
      }
    }
    poll();
    const timer = setInterval(poll, 2500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [shop, attempt, api]);
  useEffect(() => {
    api("/ads/reports")
      .then(setReports)
      .catch((e) => setError(String(e)));
  }, [shop, api]);
  async function act(fn: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <article>
        <div className="row">
          <h2>TikTok 广告账户</h2>
          <span className="badge">
            {labels[connection.status] || connection.status} · 只读
          </span>
        </div>
        <p>
          授权后可查询账户数据和广告报表。此模块不提供改预算、开关或创建广告的执行入口。
        </p>
        {connection.error && <p className="error">{connection.error}</p>}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="row">
          <button
            disabled={busy || connection.status === "connecting"}
            onClick={() =>
              act(async () => {
                const a = await api("/ads/connect", {});
                setAttempt(a.attempt_id);
                sessionStorage.setItem("ads-attempt:" + shop, a.attempt_id);
                setConnection({ status: "connecting" });
                setAuthUrl("");
              })
            }
          >
            连接 / 重新授权 TikTok
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => {
              if (
                confirm(
                  "删除本机保存的广告授权？这不会自动撤销 TikTok 侧授权，远程撤销需到 TikTok 授权设置操作。",
                )
              )
                act(async () => {
                  setConnection(
                    await api("/ads/connection", undefined, "DELETE"),
                  );
                  setAuthUrl("");
                  setAttempt("");
                  setTools([]);
                  sessionStorage.removeItem("ads-attempt:" + shop);
                });
            }}
          >
            断开并删除本地授权
          </button>
        </div>
        {connection.status === "connecting" && (
          <p>
            {authUrl ? (
              <a href={authUrl} target="_blank" rel="noreferrer">
                打开 TikTok 授权页面 →
              </a>
            ) : (
              "正在获取官方授权地址，请稍候。若服务重启或等待超时，可先断开再重试。"
            )}
          </p>
        )}
        <details>
          <summary>查看官方工具和本地只读策略</summary>
          <button
            disabled={busy || connection.status !== "connected"}
            onClick={() =>
              act(async () => {
                setTools(await api("/ads/tools"));
              })
            }
          >
            加载工具列表
          </button>
          <p>
            未声明只读的工具默认不可执行；通用执行器和写操作始终被拦截。若没有可用工具，请先核对官方定义。
          </p>
          {tools.map((t) => (
            <details key={t.name}>
              <summary>
                {t.allowed ? "可查询" : "已拦截"} · {t.name}
              </summary>
              <p>{t.description}</p>
              <pre>{JSON.stringify(t.input_schema, null, 2)}</pre>
            </details>
          ))}
        </details>
      </article>
      <div className="grid">
        <article>
          <h2>广告分析</h2>
          <p>
            广告账户 ID 可从 Ads Manager
            获取。报告只基于实际查询成功的数据；日期、币种和归因口径需结合证据核对。
          </p>
          {modelMode !== "bailian" && (
            <aside>
              当前为离线演示。请配置百炼后使用真实广告分析，不会生成模拟广告表现。
            </aside>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              act(async () => {
                const r = await api("/ads/analyze", {
                  advertiser_id: f.get("advertiser"),
                  question: f.get("question"),
                  start_date: f.get("start"),
                  end_date: f.get("end"),
                });
                setReport(r);
                setReports(await api("/ads/reports"));
              });
            }}
          >
            <label>
              广告账户 ID
              <input
                name="advertiser"
                required
                pattern="[0-9]{1,30}"
                placeholder="例如你的 advertiser_id"
              />
            </label>
            <div className="row">
              <label>
                开始日期
                <input
                  type="date"
                  name="start"
                  required
                  defaultValue={localDate(-7)}
                />
              </label>
              <label>
                结束日期
                <input
                  type="date"
                  name="end"
                  required
                  defaultValue={localDate(-1)}
                />
              </label>
            </div>
            <label>
              分析问题
              <textarea
                name="question"
                required
                maxLength={2000}
                defaultValue="查询广告表现，指出效果下降的活动，并解释数据限制和可验证的优化建议。"
              />
            </label>
            <button
              disabled={
                busy ||
                connection.status !== "connected" ||
                modelMode !== "bailian"
              }
            >
              {busy ? "查询与分析中…" : "查询真实数据并分析"}
            </button>
          </form>
        </article>
        <article>
          <h2>历史报告</h2>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              act(async () => {
                setReports(await api("/ads/reports"));
              })
            }
          >
            刷新报告
          </button>
          {reports.map((r) => (
            <div key={r.id}>
              <button
                className="session secondary"
                disabled={busy || r.expired}
                onClick={() =>
                  act(async () => {
                    setReport(await api("/ads/reports/" + r.id));
                  })
                }
              >
                {r.expired ? `报告已过期 · ${r.created_at ? new Date(r.created_at * 1000).toLocaleDateString() : r.id}` : `${r.pinned ? "已收藏 · " : ""}${r.advertiser_id} · ${r.start_date} 至 ${r.end_date}`}
              </button>
              <small>{r.expired ? "正文和证据已回收，请重新查询。" : r.question}</small>
            </div>
          ))}
        </article>
      </div>
      {report && (
        <article>
          <div className="row">
            <h2>分析结果</h2>
            {report.retention_revision != null && <button className="secondary" disabled={busy} onClick={()=>act(async()=>{
              const saved=await api(`/agent-reports/ads_report/${report.id}/retention`,{retention_revision:report.retention_revision,pinned:!report.pinned},"PUT");
              setReport({...report,...saved});
              setReports(await api("/ads/reports"));
            })}>{report.pinned ? "取消收藏" : "收藏并长期保留"}</button>}
            <button
              className="secondary"
              disabled={busy}
              onClick={() => {
                if (confirm("删除这份报告及其证据？"))
                  act(async () => {
                    await api("/ads/reports/" + report.id, undefined, "DELETE");
                    setReport(null);
                    setReports(await api("/ads/reports"));
                  });
              }}
            >
              删除报告
            </button>
          </div>
          <MetricScope.Provider value={{context:{window:report.comparison_windows?.current,previous_window:report.comparison_windows?.previous,year_ago_window:report.comparison_windows?.year_ago}}}>
          <p>广告账户：{report.advertiser_id} · 本期：{report.start_date} — {report.end_date}</p>
          <MetricHelp id="ads.report" showComparison={!report.metric_comparisons?.length}/>
          {!!report.metric_comparisons?.length && <div className="table-scroll"><table><thead><tr><th>工具返回维度</th><th>指标</th><th>本期原值</th><th>环比 / 同比</th><th>证据</th></tr></thead><tbody>{report.metric_comparisons.map((value,index)=><tr key={index}><td>{JSON.stringify(value.dimensions)}</td><td>{value.label}<MetricHelp id={value.definition} comparison={value}/></td><td>{value.value}</td><td><DeltaPair comparison={value}/></td><td>{value.evidence.join(' / ')}</td></tr>)}</tbody></table></div>}
          <pre>{report.answer}</pre></MetricScope.Provider>
          <p>工具调用 {report.tool_calls} 次；未执行广告修改。</p>
          <ul>
            {report.limitations?.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
          <details>
            <summary>展开原始工具证据与计算结果</summary>
            <pre>{JSON.stringify(report.evidence, null, 2)}</pre>
          </details>
        </article>
      )}
    </section>
  );
}
