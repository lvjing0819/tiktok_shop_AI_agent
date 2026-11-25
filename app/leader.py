"""Bounded planning/repair; workers execute policy-checked step contracts."""
import asyncio
import json
import time
from datetime import datetime, timezone
from app import model, memory, harness, execution_context
from app.tool_registry import ExecutionError


class PlanningUsage:
    def __init__(self,budget):
        self.calls=0;self.tokens=0;self.missing=0;self.budget=budget
    def model_start(self,max_tokens):
        self.calls+=1
        if self.calls>min(2,self.budget.max_model_calls):raise ExecutionError('Leader规划调用超过上限')
        if self.tokens>=self.budget.max_reported_tokens:raise ExecutionError('Leader规划达到已报告用量预算')
        return min(6000,self.budget.max_reported_tokens-self.tokens)
    def model_end(self,usage):
        n=usage.get('total_tokens') if isinstance(usage,dict) else None
        if type(n)is int and n>=0:self.tokens+=n
        else:self.missing+=1
    def remote_call(self,name):
        raise ExecutionError('规划阶段不能直接执行业务工具')


async def plan(workspace,question,budget=None,session_id=None,context=None):
    if model.mode()!='bailian':raise ExecutionError('Leader规划需要真实百炼模型；演示模式不会伪造任务计划')
    budget=budget or harness.Budget()
    reg=harness.registry();usage=PlanningUsage(budget);token=execution_context.observer.set(usage)
    started=time.time()
    try:
        messages=[{'role':'system','content':
            '你是公司内部运营Leader，负责理解目标并生成受约束任务计划，不直接操作业务。只调用submit_plan。'
            '仅使用清单中的四种角色 research/product/content/ads 与精确工具名。按依赖顺序排列，最多12步。'
            '简单查询尽量1步；需要发现ID时先发现再查询。复合任务才拆分。每步明确目标与完成标准。'
            '每步始终填写depends_on数组和bindings对象，没有则用[]与{}。绑定字段不能同时写入arguments；depends_on必须包含每个bindings引用的步骤ID。'
            '最终汇总由Leader在所有步骤完成后统一执行，不要添加假汇总步骤，不要用business.dashboard当成通用汇总器。'
            'arguments只包含用户明确给出的参数或确认过的历史条件，不能虚构内部商品ID、SKU、仓库、广告户、成本、库存。'
            '内部商品ID与外部竞品ID不同；外部选品结果需用户确认并导入商品档案，不能把竞品ID直接作为内部商品ID。'
            '不掌握店铺ID时，先用shop.discover按市场或店名读取授权店铺，再将后续shop_id绑定该步骤的selected.id；不要让用户重复提供已可发现的信息。'
            '资料缺失时可先读取business.catalog/business.inventory，再等待用户确认，无法拟定有效步骤时steps为空并填写clarification。'
            '依赖输出用bindings，每个绑定指明依赖step与data中的path；禁止猜测输出路径。商品档案列表是products，单个商品是product，content.draft输出id和revision；准备上架绑定content_draft_id时，同时将content_draft_revision绑定同一内容步骤的revision，不能猜测版本。'
            '有些能力available=false，若目标依赖这些能力须在计划中列出对应步骤并说明受阻，不可用草稿代替已发布或已生成媒体。'
            '真实发布必须人工审核；Leader没有批准权限。估算销量不是实测，GMV不是利润，TikTok展示库存不是实物库存。'
            '累计销量排序不能设置历史日期。GMV只支持1D/7D/30D，结束日按当前日期之前最近完整日选择；未知数据不填零。'
            '跨市场订单和广告应拆为各自步骤，不把不同币种直接相加。计划执行中不会任意扩大目标或预算。'},
            {'role':'user','content':json.dumps({'question':question,'current_utc':datetime.now(timezone.utc).isoformat(),'tools':reg.public(),'preferences':memory.preferences(workspace),'context':context or {}},ensure_ascii=False)}]
        tool_schema=harness.Plan.model_json_schema()
        tool_schema['$defs']['Step']['required']=['id','agent','goal','tool','arguments','bindings','depends_on','success_criteria']
        functions=[{'type':'function','function':{'name':'submit_plan','description':'提交结构化任务计划或说明需要补充的信息。','parameters':tool_schema}}]
        spec=None
        for attempt in range(min(2,budget.max_model_calls)):
            response=await asyncio.to_thread(model.message,messages,functions)
            calls=response.get('tool_calls') or []
            try:
                if len(calls)!=1 or calls[0].get('function',{}).get('name')!='submit_plan':raise ValueError('只允许一个submit_plan调用')
                spec=harness.Plan.model_validate(json.loads(calls[0]['function']['arguments']))
                harness.validate_plan(spec)
                break
            except Exception as exc:
                spec=None
                if attempt:break
                messages.append({'role':'user','content':json.dumps({'previous_submission':harness.redact(response),'validation_error':harness.redact(str(exc))[:1500],'instruction':'以上为程序校验反馈，草稿未执行。修正后重新调用submit_plan。绑定字段仅在bindings中，引用必须列入depends_on。无需汇总步骤，最终由Leader汇总。'},ensure_ascii=False)[:20000]})
        if spec is None:raise ExecutionError('Leader计划校验失败，未创建或执行业务任务；请补充更明确的目标')
        run=harness.create(workspace,question,spec,budget,session_id)
        run['usage'].update(model_calls=usage.calls,reported_tokens=usage.tokens,unreported_usage_calls=usage.missing)
        run['planning_seconds']=time.time()-started
        from app import store, agent_state
        with store.db(write=True) as conn:
            current=harness.lock(conn,run['id'],workspace)
            current['usage']['model_calls']+=usage.calls
            current['usage']['reported_tokens']+=usage.tokens
            current['usage']['unreported_usage_calls']+=usage.missing
            current['planning_seconds']=run['planning_seconds']
            agent_state.save(conn,current,workspace,bump=False)
        return current
    finally:
        execution_context.observer.reset(token)
