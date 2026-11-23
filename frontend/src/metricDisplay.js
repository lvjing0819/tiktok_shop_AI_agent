import metricCatalog from "../../app/metric_definitions.json";

const safe = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const missingLabels = {not_applicable:"不适用",different_length:"同期天数不同",partial_current:"本期不完整",missing_current:"本期未同步",partial_baseline:"基期不完整",missing_baseline:"基期未同步",unavailable_range:"该日期不可查询",failed_current:"本期读取失败",failed_baseline:"基期读取失败",missing_value:"字段未返回",missing_baseline_field:"基期字段未返回",both_zero:"均为0",zero_baseline:"基期为0",direction_changed:"正负方向变化"};

export function comparisonText(value) {
  if (!value) return "未提供基期数据";
  if (value.status !== "ok") return missingLabels[value.status] || "不可比较";
  return `${value.change > 0 ? "+" : ""}${Number(value.change).toFixed(1)}${value.unit || "%"}`;
}

export function comparisonHtml(entry) {
  return `<span class="metric-comparisons">${[["环比","previous"],["同比","year_ago"]].map(([label,key])=>{
    const value=entry?.[key];const reason=value?.reason || "";
    return `<span class="metric-trend ${value?.status === "ok" ? "known" : "unknown"}" title="${safe(reason)}" aria-label="${label}：${safe(comparisonText(value))}${reason ? "，"+safe(reason) : ""}">${label} <b>${safe(comparisonText(value))}</b></span>`;
  }).join("")}</span>`;
}

export function metricDefinitionHtml(id, context = {}) {
  const d=metricCatalog.metrics[id];if(!d)return "";
  const periods=context.window ? `<p>本期：${safe(context.window.start_date)} — ${safe(context.window.end_date)}<br>环比：${safe(context.previous_window?.start_date)} — ${safe(context.previous_window?.end_date)}<br>同比：${safe(context.year_ago_window?.start_date)} — ${safe(context.year_ago_window?.end_date)}</p>` : "";
  return `<details class="metric-definition" data-definition="${safe(id)}"><summary aria-label="查看${safe(d.label)}统计口径">口径 ⓘ</summary><div class="metric-definition-body"><strong>${safe(d.label)}</strong><p>${safe(d.formula)}</p><p>日期：${safe(d.date_basis)}</p>${periods}<p>${safe(d.limitations)}</p><p>增长率=(本期−基期)÷|基期|；比例指标比较百分点。费用规模按绝对值比较，正负变号单列。数据不全或基期为0时不计算增长率。</p></div></details>`;
}

export function metricCellHtml(entry, definition, context = {}) {
  return `<span class="metric-inline" data-metric-instance="${safe(context.instance || entry?.key || definition || entry?.definition)}">${metricDefinitionHtml(definition || entry?.definition,context)}${comparisonHtml(entry)}</span>`;
}

export function annotateMetric(element, keys, context, comparisons) {
  if(!element)return;
  const id=element.id;let annotation=element.parentElement.querySelector(`[data-metric-for="${id}"]`);
  if(!annotation){annotation=document.createElement("div");annotation.className="metric-annotation";annotation.dataset.metricFor=id;element.insertAdjacentElement("afterend",annotation);}
  const openIds = new Set([...annotation.querySelectorAll("details[open]")].map(d=>d.dataset.definition));
  annotation.innerHTML=keys.map(key=>{
    const e=comparisons?.[key];const def=e?.definition || (key.includes("|") ? key.split("|")[0]+"."+key.split("|").at(-1) : key);
    return `<div class="metric-annotation-row" data-metric-key="${safe(key)}">${keys.length>1?`<small>${safe(e?.label || metricCatalog.metrics[def]?.label || key)}</small>`:""}${metricCellHtml(e,def,context)}</div>`;
  }).join("");
  for(const details of annotation.querySelectorAll("details")){details.open=openIds.has(details.dataset.definition);}
}

export function dimensionTableHtml(entries, context) {
  return `<details class="dimension-comparisons"><summary>查看各项环比 / 同比与口径</summary><div class="table-wrap"><table><thead><tr><th>维度</th><th>本期</th><th>前一周期</th><th>去年同期</th><th>环比 / 同比</th></tr></thead><tbody>${entries.map(e=>`<tr><td>${safe(e.label)}${metricDefinitionHtml(e.definition,context)}</td><td>${safe(e.value ?? "—")}</td><td>${safe(e.previous?.baseline ?? "—")}</td><td>${safe(e.year_ago?.baseline ?? "—")}</td><td>${comparisonHtml(e)}</td></tr>`).join("")}</tbody></table></div></details>`;
}

export function pageGuideHtml(page) {
  const d=metricCatalog.pages[page];return d?`<details class="page-metric-guide"><summary>${safe(d.title)} ⓘ</summary><p>${safe(d.text)}</p></details>`:"";
}
