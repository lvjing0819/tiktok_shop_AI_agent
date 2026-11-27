"""Owner-authenticated result reads, retention previews and reviewed memory."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from typing import Literal
from uuid import UUID
from fastapi.responses import Response
from app import agent_artifacts, agent_maintenance, memory, store, harness, report_retention, sandbox, sandbox_files
from app.tool_registry import ExecutionError


class Cleanup(harness.Strict):
    preview_id: str
    confirmation: str


class ReportPin(harness.Strict):
    retention_revision: int = Field(ge=1)
    pinned: bool


class FileRetention(harness.Strict):
    revision: int = Field(ge=1)
    pinned: bool


class Candidate(harness.Strict):
    field: Literal['market','brand_style','forbidden_claims','notes']
    value: str = Field(min_length=1,max_length=2000)
    session_id: str
    message_index: int = Field(ge=0)


class Review(harness.Strict):
    revision: int = Field(ge=1)
    approved: bool


class DraftEdit(harness.Strict):
    revision: int = Field(ge=1)
    title: str | None = Field(default=None,min_length=1,max_length=255)
    description: str | None = Field(default=None,min_length=1,max_length=10000)
    image_briefs: list[str] | None = Field(default=None,min_length=1,max_length=6)
    video_script: str | None = Field(default=None,max_length=8000)
    claims_to_verify: list[str] | None = Field(default=None,max_length=20)


class DraftRevision(harness.Strict):
    revision: int = Field(ge=1)


def router(scope):
    r=APIRouter(tags=['Agent runtime and retention'])
    @r.get('/api/agent-files/{key}')
    def file_metadata(key:UUID,workspace=Depends(scope)):
        try:
            item=sandbox_files.metadata(str(key),workspace)
            if item['state']=='staged':raise LookupError('文件尚未交付')
            return item
        except LookupError as e:raise HTTPException(404,str(e))
    @r.get('/api/agent-files/{key}/download')
    def file_download(key:UUID,workspace=Depends(scope)):
        try:item,body=sandbox_files.download(str(key),workspace)
        except LookupError as e:raise HTTPException(404,str(e))
        except sandbox_files.FileError as e:raise HTTPException(410 if e.code=='FILE_EXPIRED' else 409,str(e))
        return Response(body,media_type=item['media_type'],headers={'Content-Disposition':'attachment; filename="'+item['name']+'"','X-Content-Type-Options':'nosniff','Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'"})
    @r.put('/api/agent-files/{key}/retention')
    def file_retention(key:UUID,body:FileRetention,workspace=Depends(scope)):
        try:return sandbox_files.pin(str(key),workspace,body.revision,body.pinned)
        except LookupError as e:raise HTTPException(404,str(e))
        except sandbox_files.FileError as e:raise HTTPException(410 if e.code=='FILE_EXPIRED' else 409,str(e))
    @r.get('/api/agent-files/{key}/events')
    def file_events(key:UUID,workspace=Depends(scope)):
        try:sandbox_files.metadata(str(key),workspace)
        except LookupError as e:raise HTTPException(404,str(e))
        return sandbox_files.history(workspace,file_id=str(key))
    @r.get('/api/operations/{key}/file-events')
    def task_file_events(key:str,workspace=Depends(scope)):
        try:harness.get(key,workspace)
        except LookupError as e:raise HTTPException(404,str(e))
        return sandbox_files.history(workspace,run_id=key)
    @r.get('/api/sandbox/status')
    async def sandbox_status(workspace=Depends(scope)):
        return await sandbox.status()
    @r.get('/api/operations/{key}/sandboxes')
    def sandbox_runs(key:str,workspace=Depends(scope)):
        try:harness.get(key,workspace)
        except LookupError as e:raise HTTPException(404,str(e))
        return sorted((item for item in store.list_records('sandbox_run',workspace) if item['run_id']==key),key=lambda i:i['created_at'])[-100:]
    @r.get('/api/agent-artifacts/{key}')
    def artifact(key:str,workspace=Depends(scope)):
        item=store.get(key,'agent_artifact',workspace)
        if not item:raise HTTPException(404,'交付物不存在')
        try:payload=agent_artifacts.read({'id':key,'version':item['version'],'checksum':item['checksum']},workspace)
        except ExecutionError as e:raise HTTPException(410,str(e))
        return {'id':key,'version':item['version'],'created_at':item['created_at'],'bytes':item['bytes'],**payload}
    @r.put('/api/agent-drafts/content/{key}')
    def revise_content(key:str,body:DraftEdit,workspace=Depends(scope)):
        changes=body.model_dump(exclude={'revision'},exclude_none=True)
        if any(len(v)>2000 for v in changes.get('image_briefs',[])) or any(len(v)>1000 for v in changes.get('claims_to_verify',[])):
            raise HTTPException(422,'素材需求或待核查声明过长')
        try:return agent_artifacts.revise_content(workspace,key,body.revision,changes)
        except LookupError as e:raise HTTPException(404,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.post('/api/agent-drafts/{kind}/{key}/retire')
    def retire(kind:Literal['content_draft','listing_draft'],key:str,body:DraftRevision,workspace=Depends(scope)):
        try:return agent_artifacts.retire_draft(workspace,kind,key,body.revision)
        except LookupError as e:raise HTTPException(404,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.put('/api/agent-reports/{kind}/{key}/retention')
    def pin_report(kind:Literal['ads_report','social_report'],key:str,body:ReportPin,workspace=Depends(scope)):
        try:return report_retention.pin(key,kind,workspace,body.retention_revision,body.pinned)
        except LookupError as e:raise HTTPException(404,str(e))
        except report_retention.ReportExpired as e:raise HTTPException(410,str(e))
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.post('/api/agent-maintenance/preview')
    def preview(policy:agent_maintenance.Policy,workspace=Depends(scope)):
        return agent_maintenance.preview(workspace,policy)
    @r.post('/api/agent-maintenance/apply')
    def apply(body:Cleanup,workspace=Depends(scope)):
        try:return agent_maintenance.apply(workspace,body.preview_id,body.confirmation)
        except ExecutionError as e:raise HTTPException(409,str(e))
    @r.get('/api/memory/candidates')
    def candidates(workspace=Depends(scope)):
        return sorted(store.list_records('memory_candidate',workspace),key=lambda c:c['created_at'],reverse=True)[:100]
    @r.post('/api/memory/candidates',status_code=201)
    def propose(body:Candidate,workspace=Depends(scope)):
        try:return memory.propose(workspace,**body.model_dump())
        except ValueError as e:raise HTTPException(422,str(e))
    @r.post('/api/memory/candidates/{key}/review')
    def review(key:str,body:Review,workspace=Depends(scope)):
        try:return memory.review_candidate(workspace,key,body.revision,body.approved)
        except LookupError as e:raise HTTPException(404,str(e))
        except RuntimeError as e:raise HTTPException(409,str(e))
    return r
