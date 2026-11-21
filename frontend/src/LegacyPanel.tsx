import { useState, useEffect } from "react";
import type { Api } from "./types";
import { Icon, StatusBadge, RichText, EmptyState } from "./ui";
export default function LegacyPanel({ api }: { api: Api }) {
  const [tasks, setTasks] = useState<any[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function refresh() {
    setTasks(await api("/tasks"));
  }
  useEffect(() => {
    refresh().catch((e) => setError(String(e)));
    const t = setInterval(() => refresh().catch(() => {}), 4000);
    return () => clearInterval(t);
  }, [api]);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">LEGACY DEMO</div>
          <h1>历史演示任务</h1>
          <p>这里使用模拟素材和模拟上架，真实运营准备请在任务中心创建。</p>
        </div>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <details>
        <summary>创建模拟商品任务</summary>
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
            });
          }}
        >
          {[
            ["name", "商品名称", ""],
            ["sku", "SKU", ""],
            ["cost", "成本（美元）", ""],
            ["stock", "输入数量", ""],
            ["margin", "目标毛利率 %", "30"],
          ].map(([name, label, value]) => (
            <label key={name}>
              {label}
              <input
                name={name}
                defaultValue={value}
                required
                type={
                  ["cost", "stock", "margin"].includes(name) ? "number" : "text"
                }
                step={name === "stock" ? "1" : "any"}
                min={name === "cost" ? "0.01" : "0"}
                max={name === "margin" ? "94.9" : undefined}
              />
            </label>
          ))}
          <button disabled={busy}>准备模拟方案</button>
        </form>
      </details>
      {tasks.length ? (
        tasks.map((t) => (
          <article key={t.id}>
            <div className="panel-heading">
              <h2>
                {t.product.name} / {t.product.sku}
              </h2>
              <StatusBadge status={t.status} />
            </div>
            {t.price && <p>示例价格：${t.price}（仅含商品成本）</p>}
            <RichText text={t.copy || "暂无文案"} />
            {t.error && <p className="error">{t.error}</p>}
            <details>
              <summary>历史执行记录</summary>
              <ul>
                {t.events.map((x: string, i: number) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </details>
            {t.status === "awaiting_review" && (
              <div className="button-group">
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
                  <Icon name="check" size={15} />
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
                className="secondary"
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    await api(`/tasks/${t.id}/retry`, {});
                  })
                }
              >
                重试未完成步骤
              </button>
            )}
          </article>
        ))
      ) : (
        <EmptyState icon="tasks" title="没有历史演示任务" />
      )}
    </>
  );
}
