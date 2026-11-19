import asyncio
import hashlib
import logging
import time
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
from app import ads_mcp, ads_analysis, store, model


# OAuth callback URLs carry an authorization code; never log their query strings.
class NoOAuthAccessLog(logging.Filter):
    def filter(self, record):
        return not any(path in record.getMessage() for path in ('/api/ads/oauth/callback', '/api/social/oauth/callback'))


logging.getLogger('uvicorn.access').addFilter(NoOAuthAccessLog())
background = set()


class AdsQuery(BaseModel):
    advertiser_id: str = Field(pattern=r'^\d{1,30}$')
    question: str = Field(min_length=1, max_length=2000)
    start_date: date
    end_date: date

    @model_validator(mode='after')
    def valid_dates(self):
        if not 0 <= (self.end_date-self.start_date).days <= 90:
            raise ValueError('查询起止日期须按顺序，跨度不超过90天')
        return self


def router(scope):
    r = APIRouter(prefix='/api/ads', tags=['Ads MCP'])

    @r.get('/connection')
    def status(shop=Depends(scope)):
        return ads_mcp.public_status(shop)

    @r.post('/connect', status_code=202)
    async def connect(shop=Depends(scope)):
        try:
            attempt, fresh = ads_mcp.begin_connection(shop)
            if fresh:
                task = asyncio.create_task(ads_mcp.authorize(shop, attempt['id'], attempt['generation']))
                background.add(task)
                task.add_done_callback(background.discard)
            return {'attempt_id': attempt['id'], 'expires_at': attempt['expires_at']}
        except ads_mcp.AdsError as exc:
            raise HTTPException(400, str(exc))

    @r.get('/attempts/{attempt_id}')
    def attempt_status(attempt_id: str, shop=Depends(scope)):
        a = store.get(attempt_id, 'ads_attempt', shop)
        if not a:
            raise HTTPException(404, '授权会话不存在')
        return {'authorization_url': a.get('authorization_url') if a['expires_at']>time.time() else None, 'expires_at': a['expires_at'], **ads_mcp.public_status(shop)}

    @r.get('/oauth/callback', response_class=HTMLResponse)
    def callback(state: str = Query(min_length=16, max_length=1000), code: str | None = Query(default=None, min_length=1, max_length=4000), error: str | None = Query(default=None, max_length=200)):
        try:
            if not code and not error:
                raise ads_mcp.AdsError('授权回调缺少结果')
            ads_mcp.accept_callback(state, code, denied=bool(error))
            if error:
                return HTMLResponse('<meta charset="utf-8"><p>未完成授权。请返回工作台重新连接。</p>', headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
            return HTMLResponse('<meta charset="utf-8"><title>授权已接收</title><p>授权已接收。请关闭此窗口，返回工作台查看连接状态。</p>', headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        except ads_mcp.AdsError as exc:
            raise HTTPException(400, str(exc))

    @r.delete('/connection')
    def disconnect(shop=Depends(scope)):
        return ads_mcp.disconnect(shop)

    @r.get('/tools')
    async def list_tools(shop=Depends(scope)):
        try:
            async with ads_mcp.session(shop) as mcp:
                found = await ads_mcp.tools(mcp)
                return [{'name': t['name'], 'description': t.get('description', ''), 'allowed': ads_mcp.permitted(t), 'input_schema': t['inputSchema']} for t in found]
        except ads_mcp.AdsError as exc:
            raise HTTPException(502, str(exc))

    @r.post('/analyze')
    async def analyze(body: AdsQuery, shop=Depends(scope)):
        try:
            async with asyncio.timeout(240):
                return await ads_analysis.analyze(shop, body.advertiser_id, body.question, body.start_date.isoformat(), body.end_date.isoformat())
        except (ads_mcp.AdsError, model.ModelError) as exc:
            raise HTTPException(502, str(exc))
        except TimeoutError:
            raise HTTPException(504, '分析超时，请缩小查询范围后重试')

    @r.get('/reports')
    def reports(shop=Depends(scope)):
        return [{k:v for k,v in report.items() if k not in ('evidence','answer')} for report in store.list_records('ads_report',shop)]

    @r.get('/reports/{report_id}')
    def report(report_id: str, shop=Depends(scope)):
        result = store.get(report_id,'ads_report',shop)
        if not result:
            raise HTTPException(404,'分析报告不存在')
        return result

    @r.delete('/reports/{report_id}')
    def remove_report(report_id: str, shop=Depends(scope)):
        store.remove(report_id,'ads_report',shop)
        return {'deleted':True}

    return r
