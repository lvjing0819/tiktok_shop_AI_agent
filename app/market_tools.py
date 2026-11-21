"""Official market rankings + eligible affiliate catalog, with explicit coverage."""
import json
import re
import time
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
from zoneinfo import ZoneInfo
from app import shop_api as api, shop_read

RANKING = '/analytics/202511/products/bestselling'
AFFILIATE = '/affiliate_seller/202405/open_collaborations/products/search'
CATEGORIES = '/product/202309/categories'


async def request(workspace, selected, path, filters, body=None):
    if path not in (RANKING, AFFILIATE, CATEGORIES):raise api.ShopError('未开放的选品接口')
    cfg=api.config();encoded=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode() if body is not None else b''
    for attempt in range(2):
        tokens,generation=await api.access(workspace,cfg,force=bool(attempt))
        sealed=api.unseal(api.connection(workspace)['sealed'],cfg)
        row=next((r for r in sealed.get('shops',[]) if r['id']==selected['id']),None)
        if not row:raise api.ShopError('当前授权中没有所选市场店铺')
        required='data.bestselling.public.read' if path==RANKING else 'seller.affiliate_collaboration.read' if path==AFFILIATE else 'seller.product.basic'
        if required not in tokens.get('granted_scopes',[]):raise api.ShopError('缺少接口所需权限：'+required)
        params={'app_key':cfg['app_key'],'timestamp':str(int(time.time())),'shop_cipher':row['cipher'],**filters}
        params['sign']=api.sign(path,params,encoded,cfg['app_secret'])
        try:
            value=await api.request_json(api.API_ROOT+path,params=params,method='POST' if body is not None else 'GET',content=encoded if body is not None else None,headers={'x-tts-access-token':tokens['access_token'],'Content-Type':'application/json'})
        except api.ShopAPIError as exc:
            if exc.code==105002 and attempt==0:continue
            raise
        with api.store.db() as conn:api.locked(conn,workspace,generation,cfg)
        return value


def source(path,value,filters,page=None):
    return {'tool':path,'request_id':value.get('request_id'),'filters':filters,'page':page,'retrieved_at':time.time(),'api_success':True}


def estimate(gmv,price,currency):
    if not isinstance(price,dict) or price.get('currency')!=currency:return None
    try:
        numbers=re.findall(r'\d[\d,]*(?:\.\d+)?',gmv)
        if len(numbers)!=2:return None
        lo,hi=[Decimal(v.replace(',','')) for v in numbers]
        pmin,pmax=Decimal(price['minimum_amount']),Decimal(price['maximum_amount'])
        if not (lo.is_finite() and hi.is_finite() and pmin.is_finite() and pmax.is_finite() and 0<=lo<=hi and 0<pmin<=pmax):return None
        return {'lower':int((lo/pmax).to_integral_value(rounding=ROUND_FLOOR)),'upper':int((hi/pmin).to_integral_value(rounding=ROUND_CEILING)),'kind':'scenario_estimate','method':'GMV下限÷当前最高促销价至GMV上限÷当前最低促销价；不是实测销量或置信区间'}
    except (InvalidOperation,KeyError,ValueError,TypeError):return None


async def search(workspace,args):
    region=args['market'];mode=args.get('ranking','gmv');limit=args.get('limit',30);warnings=[];sources=[]
    discovery,selected=await shop_read.discover(workspace,region)
    if not selected:return {'status':'waiting_input','data':{'shops':discovery['shops']},'summary':'没有唯一匹配的授权市场店铺，请补充店铺或完成对应市场授权。','data_verified':False,'evidence':[]}
    target=args.get('category_id');root=target;name=None
    if target:
        categories=await request(workspace,selected,CATEGORIES,{'locale':'en-US','category_version':'v2'})
        sources.append(source(CATEGORIES,categories,{'category_version':'v2'}))
        byid={r['id']:r for r in categories['data'].get('categories',[])}
        node=byid.get(target)
        if not node:return {'status':'waiting_input','data':{},'summary':'未找到该市场类目ID，请确认类目。','data_verified':False,'evidence':sources}
        name=node.get('local_name');seen=set()
        while node.get('parent_id') not in (None,'0'):
            if node['id'] in seen or node.get('parent_id') not in byid:raise api.ShopError('类目层级不完整，不能推断上级类目')
            seen.add(node['id']);node=byid[node['parent_id']]
        root=node['id']
    zone=shop_read.MARKET_ZONES.get(region)
    if mode=='gmv' and not args.get('end_date') and not zone:
        return {'status':'waiting_input','data':{},'summary':'请明确该市场榜单的结束日期，避免按未知时区推断昨天。','data_verified':False,'evidence':sources}
    rankings=[];ending=args.get('end_date') or (datetime.now(ZoneInfo(zone or 'UTC')).date()-timedelta(days=1)).isoformat()
    if mode=='cumulative' and args.get('end_date'):
        return {'status':'waiting_input','data':{},'summary':'累计销量排序不支持指定历史日期，请删除end_date；近7天GMV排序使用gmv。','data_verified':False,'evidence':sources}
    if mode=='gmv':
        try:date.fromisoformat(ending)
        except ValueError:raise api.ShopError('榜单结束日期无效') from None
        filters={'date':ending,'time_slot':args.get('time_slot','7D'),'currency':'LOCAL'}
        if root:filters['category_id']=root
        d=await request(workspace,selected,RANKING,filters);sources.append(source(RANKING,d,filters))
        rankings=d['data'].get('products',[])
        if not isinstance(rankings,list):raise api.ShopError('榜单返回格式异常')
        if target and target!=root:warnings.append('官方GMV接口仅按一级类目查询；下面是其Top100内可识别的目标类目候选，不是完整细分类目榜。')
    affiliate={};token='';seen_tokens=set();regions=set();exhausted=False;scanned=0
    filters={'page_size':'20','sort_field':'units_sold','sort_order':'DESC'}
    body={}
    # Both official endpoints accept first-level categories only.
    if root:body['category']={'id':root}
    if target and target!=root and mode=='cumulative':
        warnings.append('联盟接口仅支持一级类目过滤；按累计销量顺序扫描最多100个商品，再根据官方类目链筛选目标细分类目。')
    if args.get('keywords'):body['title_keywords']=args['keywords']
    # At most 100 affiliate products. Missing matching records remain unknown.
    for page in range(5):
        query={**filters,**({'page_token':token} if token else {})}
        d=await request(workspace,selected,AFFILIATE,query,body);sources.append(source(AFFILIATE,d,{'ranking':'cumulative','category_id':root,'requested_leaf_category_id':target,'keywords':args.get('keywords',[])},page+1))
        rows=d['data'].get('products',[])
        if not isinstance(rows,list):raise api.ShopError('联盟商品返回格式异常')
        for r in rows:
            scanned+=1
            regions.add(str(r.get('sale_region')))
            if r.get('sale_region')==region and isinstance(r.get('id'),str):
                if target and target!=root and not any(c.get('id')==target for c in r.get('category_chains',[])):
                    continue
                affiliate[r['id']]=r
        if rows and region not in regions:
            warnings.append('联盟商品接口返回其他市场商品，当前市场的价格与累计销量不能据此补齐。')
            break
        token=d['data'].get('next_page_token') or ''
        if not token:exhausted=True;break
        if token in seen_tokens:warnings.append('重复游标，已停止翻页。');break
        seen_tokens.add(token)
        if mode=='cumulative' and len(affiliate)>=limit:break
    if mode=='cumulative':
        rankings=[{'id':r['id'],'name':r.get('title'),'shop_name':(r.get('shop') or {}).get('name'),'rank':i+1,'product_image':{'urls':[r['main_image_url']]} if r.get('main_image_url') else {}} for i,r in enumerate(affiliate.values())][:limit]
        if not rankings and regions and region not in regions:
            return {'status':'waiting_input','data':{'returned_regions':sorted(regions)},'summary':'当前联盟查询未返回目标市场商品；需要核对该市场的联盟授权，不能将其他市场商品作为结果。','data_verified':False,'evidence':sources}
    currency={'MY':'MYR','TH':'THB','US':'USD','GB':'GBP','SG':'SGD','VN':'VND','PH':'PHP','JP':'JPY'}.get(region)
    products=[]
    for r in rankings:
        if mode=='gmv' and args.get('keywords') and not all(k.casefold() in str(r.get('name','')).casefold() for k in args['keywords']):
            continue
        matched=affiliate.get(r['id']);chain=matched.get('category_chains',[]) if matched else []
        verified=bool(target and any(c.get('id')==target for c in chain))
        classification='official_category_chain' if verified else 'first_level_ranking' if root==target else 'unknown'
        if mode=='gmv' and target and target!=root and not verified:
            title=str(r.get('name','')).lower()
            inferred=target=='605254' and bool(re.search(r'watch|jam|นาฬิกา|手表',title)) and bool(re.search(r'wom[ae]n|lad(y|ies)|female|wanita|perempuan|ผู้หญิง|สตรี|女士',title)) and not bool(re.search(r'(?<![a-z])men(?![a-z])|lelaki|ผู้ชาย|unisex|couple|情侣|男女',title))
            if not inferred:continue
            classification='title_candidate_only'
        price=matched.get('sales_price') if matched else None
        units=matched.get('units_sold') if matched else None
        if type(units) is not int or units<0:units=None
        estimated=estimate(r.get('gmv_range',''),price,currency) if mode=='gmv' else None
        products.append({'id':r['id'],'name':r.get('name'),'shop_name':r.get('shop_name'),'rank':r.get('rank'),'rank_scope':'eligible_affiliate_cumulative_sales' if mode=='cumulative' else 'official_first_level_gmv_ranking' if root else 'official_market_gmv_ranking','market':region,'category_id':target,'category_name':name,'category_verification':classification,'product_image':r.get('product_image'),'gmv_range':r.get('gmv_range'),'gmv_currency':currency if mode=='gmv' else None,'sales_price':price,'cumulative_units_sold':units,'estimated_units':estimated,'estimate_exceeds_cumulative':bool(estimated and units is not None and estimated['upper']>units)})
        if len(products)>=limit:break
    if mode=='gmv' and args.get('keywords'):warnings.append('关键词仅筛选官方GMV Top100中的标题，不能代表全市场关键词榜。')
    if len(products)<limit:warnings.append('可识别结果不足请求条数；未使用其他类目或估算记录凑数。')
    if any(r['estimated_units'] for r in products):warnings.append('销量估算使用当前促销价和脱敏GMV，不代表历史实际销量；估算上限可能超过累计销量。')
    data={'market':region,'ranking':mode,'query_category_id':root,'affiliate_scanned_count':scanned,'end_date':ending if mode=='gmv' else None,'time_slot':args.get('time_slot','7D') if mode=='gmv' else None,'requested_count':limit,'returned_count':len(products),'products':products,'complete_target_ranking':(mode=='cumulative' and (exhausted or len(products)>=limit)) or (mode=='gmv' and (not target or target==root) and not args.get('keywords') and len(products)>=limit),'warnings':warnings,'source_request_ids':[s['request_id'] for s in sources]}
    return {'status':'completed','data':data,'data_verified':True,'summary':f'实际返回{len(products)}个商品；保留榜单范围与缺失字段。','evidence':sources}
