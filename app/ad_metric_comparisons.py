"""Compare only matching, successful ad-tool rows with identical query scope."""
import json
from calendar import monthrange
from datetime import date, timedelta
from app import metrics

FIELDS = {'cost':'GMV Max 广告消耗','spend':'广告消耗','impressions':'广告展示次数','clicks':'广告点击次数','conversion':'平台归因转化数'}


def windows(start_date, end_date):
    start=date.fromisoformat(start_date);end=date.fromisoformat(end_date);length=(end-start).days+1
    shift=lambda d:d.replace(year=d.year-1,day=min(d.day,monthrange(d.year-1,d.month)[1]))
    return {'current':{'start_date':str(start),'end_date':str(end)},
            'previous':{'start_date':str(start-timedelta(days=length)),'end_date':str(start-timedelta(days=1))},
            'year_ago':{'start_date':str(shift(start)),'end_date':str(shift(end))}}


def payload(result):
    value=result.get('structuredContent')
    if not isinstance(value,dict):
        blocks=result.get('content') or []
        if len(blocks)!=1 or blocks[0].get('type')!='text':return None
        try:value=json.loads(blocks[0]['text'])
        except (KeyError,TypeError,ValueError):return None
    return value.get('data',value) if isinstance(value,dict) else None


def build(evidence, periods):
    grouped={}
    for item in evidence:
        if item.get('is_error') or not item.get('date_filter_in_schema'):continue
        args=item['arguments'];period=next((key for key,value in periods.items() if args.get('start_date')==value['start_date'] and args.get('end_date')==value['end_date']),None)
        if not period:continue
        data=payload(item['result'])
        if not isinstance(data,dict) or not isinstance(data.get('list'),list):continue
        page=data.get('page_info') or {}
        total_pages=metrics.number(page.get('total_page',1))
        partial=bool(data.get('next_page_token')) or total_pages is None or total_pages>1 or len(data['list'])>100
        # Parameters other than dates must match, including filters, attribution,
        # timezone, currency and pagination. Never merge different reports.
        scope=json.dumps({k:v for k,v in args.items() if k not in ('start_date','end_date')},sort_keys=True,ensure_ascii=False)
        for row in data['list'][:100]:
            if not isinstance(row,dict) or not isinstance(row.get('metrics'),dict):continue
            dimensions=row.get('dimensions') or {}
            fingerprint=(item['tool'],scope,json.dumps(dimensions,sort_keys=True,ensure_ascii=False))
            for field,label in FIELDS.items():
                value=row['metrics'].get(field)
                if metrics.number(value) is None:continue
                key=(*fingerprint,field);record=grouped.setdefault(key,{'field':field,'label':label,'dimensions':dimensions,'definition':'ads.'+field,'periods':{}})
                old=record['periods'].get(period)
                record['periods'][period]={'value':value,'status':'partial_detail' if partial or old else 'complete','evidence':item['evidence_id']}
    result=[]
    year_length=(date.fromisoformat(periods['year_ago']['end_date'])-date.fromisoformat(periods['year_ago']['start_date'])).days
    current_length=(date.fromisoformat(periods['current']['end_date'])-date.fromisoformat(periods['current']['start_date'])).days
    for record in grouped.values():
        current=record['periods'].get('current')
        if not current:continue
        row={k:v for k,v in record.items() if k!='periods'};row['value']=str(current['value']);row['evidence']=[current['evidence']]
        for name in ('previous','year_ago'):
            baseline=record['periods'].get(name,{})
            row[name]=metrics.compare(current['value'],baseline.get('value'),current_status=current['status'],baseline_status=baseline.get('status','missing'),same_length=name!='year_ago' or year_length==current_length)
            if baseline:row['evidence'].append(baseline['evidence'])
        result.append(row)
    return result
