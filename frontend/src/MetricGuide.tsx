import { createContext, useContext } from "react";
import catalog from "../../app/metric_definitions.json";

type Delta = {status:string;reason?:string;change?:number|null;unit?:string;current?:string|null;baseline?:string|null;baseline_day?:string;baseline_recorded_at?:number|null};
export type MetricComparison = {previous?:Delta;year_ago?:Delta};
type Window = {start_date:string;end_date:string};
export const MetricScope = createContext<{comparisons?:Record<string,MetricComparison>;context?:{window?:Window;previous_window?:Window;year_ago_window?:Window}}>({});
const definitions=catalog.metrics as Record<string,{label:string;formula:string;date_basis:string;limitations:string;comparison:string}>;
const aliases=catalog.labels as Record<string,string>;
const states:Record<string,string>={not_applicable:"不适用",different_length:"同期天数不同",partial_current:"本期不完整",missing_current:"本期未同步",partial_baseline:"基期不完整",missing_baseline:"基期未同步",unavailable_range:"该日期不可查询",failed_current:"本期读取失败",failed_baseline:"基期读取失败",missing_value:"字段未返回",missing_baseline_field:"基期字段未返回",both_zero:"均为0",zero_baseline:"基期为0",direction_changed:"正负方向变化"};

export function DeltaPair({comparison}:{comparison?:MetricComparison}) {
  return <span className="metric-comparisons">{([['环比','previous'],['同比','year_ago']] as const).map(([label,key])=>{
    const value=comparison?.[key];const text=value?.status==='ok'?`${(value.change || 0)>0?'+':''}${Number(value.change).toFixed(1)}${value.unit || '%'}`:states[value?.status || ''] || '基期未留存';
    return <span key={key} className={'metric-trend '+(value?.status==='ok'?'known':'unknown')} title={(value?.baseline_day ? "比较日期："+value.baseline_day+"。" : "")+(value?.reason || "同口径已保存数据比较")} aria-label={label+'：'+text}>{label} <b>{text}</b></span>;
  })}</span>;
}

export function MetricHelp({id,label,comparison,showComparison=true}:{id?:string;label?:string;comparison?:MetricComparison;showComparison?:boolean}) {
  const scope=useContext(MetricScope);const key=id || aliases[label || ''];const d=definitions[key];
  if(!d)return null;
  const value=comparison || scope.comparisons?.[key];
  return <div className="metric-annotation"><details className="metric-definition"><summary aria-label={'查看'+d.label+'统计口径'}>口径 ⓘ</summary><div className="metric-definition-body"><strong>{d.label}</strong><p>{d.formula}</p><p>{d.date_basis}</p>{!key.startsWith("inventory.") && !key.startsWith("operations.") && scope.context?.window && <p>本期：{scope.context.window.start_date} — {scope.context.window.end_date}<br/>环比：{scope.context.previous_window?.start_date} — {scope.context.previous_window?.end_date}<br/>同比：{scope.context.year_ago_window?.start_date} — {scope.context.year_ago_window?.end_date}</p>}<p>{d.limitations}</p></div></details>{showComparison && <DeltaPair comparison={value || (d.comparison==='none'?{previous:{status:'not_applicable',reason:d.limitations},year_ago:{status:'not_applicable',reason:d.limitations}}:undefined)}/>}</div>;
}

export function PageMetricGuide({page}:{page:string}) {
  const guide=(catalog.pages as Record<string,{title:string;text:string}>)[page];
  return guide?<details className="page-metric-guide"><summary>{guide.title} ⓘ</summary><p>{guide.text}</p><p>环比为前一等长周期，同比为去年同期；百分比=(本期−基期)÷|基期|。比例比较显示百分点；费用和负净结算额按绝对金额规模比较，正负变号单列。未知不补0，基期为0时不计算增长率。库存比较前一日及去年同日的最后已留存快照。</p></details>:null;
}
