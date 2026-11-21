from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from app import harness, leader, store, model
from app.tool_registry import ExecutionError


class PlanRequest(harness.Strict):
    question: str = Field(min_length=1,max_length=4000)
    budget: harness.Budget = Field(default_factory=harness.Budget)


class Revision(harness.Strict):
    revision: int = Field(ge=1)


class Revise(Revision):
    plan: harness.Plan


class Approval(Revision):
    approved: bool


def router(scope):
    r=APIRouter(tags=['Leader and business harness'])
    @r.get('/api/operations/tools')
    def tools(workspace=Depends(scope)):
        return {'tools':harness.registry().public(),'registry_version':'1','hot_reload':False}
    @r.get('/api/operations/metrics')
    def metrics(workspace=Depends(scope)):
        return harness.metrics(workspace)
    @r.get('/api/operations')
    def runs(workspace=Depends(scope)):
        rows=store.list_records('operation_run',workspace)
        return [{k:v for k,v in item.items() if k not in ('steps','events','lease_owner')} for item in sorted(rows,key=lambda x:x['created_at'],reverse=True)[:100]]
    @r.post('/api/operations/plan',status_code=201)
    async def create(body:PlanRequest,workspace=Depends(scope)):
        try:return await leader.plan(workspace,body.question,body.budget)
        except (ExecutionError,model.ModelError) as e:raise HTTPException(502,str(e))
    @r.get('/api/operations/{key}')
    def detail(key:str,workspace=Depends(scope)):
        try:return {k:v for k,v in harness.get(key,workspace).items() if k!='lease_owner'}
        except LookupError as e:raise HTTPException(404,str(e))
    @r.get('/api/operations/{key}/traces')
    def traces(key:str,workspace=Depends(scope)):
        try:harness.get(key,workspace)
        except LookupError as e:raise HTTPException(404,str(e))
        return sorted([t for t in store.list_records('tool_trace',workspace) if t['run_id']==key],key=lambda x:x['started_at'])
    @r.post('/api/operations/{key}/start',status_code=202)
    def start(key:str,body:Revision,workspace=Depends(scope)):
        try:return harness.start(workspace,key,body.revision)
        except LookupError as e:raise HTTPException(404,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.put('/api/operations/{key}/plan')
    def revise(key:str,body:Revise,workspace=Depends(scope)):
        try:return harness.revise(workspace,key,body.revision,body.plan)
        except LookupError as e:raise HTTPException(404,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.post('/api/operations/{key}/approval',status_code=202)
    def approve(key:str,body:Approval,workspace=Depends(scope)):
        try:return harness.approve(workspace,key,body.revision,body.approved)
        except LookupError as e:raise HTTPException(404,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    return r
