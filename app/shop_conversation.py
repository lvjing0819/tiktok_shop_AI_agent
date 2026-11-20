"""Shop selection/clarification; deterministic reads reuse the HTTP adapter."""
from pydantic import ValidationError
from app import shop_api, shop_read


def discovery_evidence(data):
    return shop_read.evidence('shops', {'code': 0, 'request_id': data.get('request_id'), 'data': {'shops': data['shops']}}, {})


async def query(workspace, args, context, run):
    pending = context.get('pending_shop') or {}
    previous = context.get('last_shop') or {}
    params = {**pending, **args}
    if args.get('relative_date'):
        params.pop('start_date', None)
        params.pop('end_date', None)
    elif args.get('start_date') or args.get('end_date'):
        params.pop('relative_date', None)
        for k in ('start_date', 'end_date'):
            if k not in args:
                params.pop(k, None)
    if args.get('reuse_previous_dates') and not any(args.get(k) for k in ('relative_date', 'start_date', 'end_date')):
        params.update({k: previous[k] for k in ('relative_date', 'start_date', 'end_date', 'timezone') if previous.get(k)})
        if params.get('relative_date'):
            params.pop('start_date', None)
            params.pop('end_date', None)
    selector = params.get('shop') or (previous.get('shop') if args.get('reuse_previous_shop') else None)
    data, selected = await shop_read.discover(workspace, selector)
    run['evidence'] = [discovery_evidence(data)]
    if not selected:
        context['pending_shop'] = params
        lines = [f"- {s['name']}（{s['region']}，{s['id']}）" for s in data['shops']]
        return '请明确选择本次查询的店铺：\n' + ('\n'.join(lines) or '当前授权没有可访问店铺。'), 'needs_input'
    params['shop'] = selected['id']
    resources = params['resources']
    unsupported = None
    if 'orders' not in resources and any(params.get(k) for k in ('relative_date', 'start_date', 'end_date')):
        unsupported = '商品目录和库存只能查询当前状态，日期筛选目前仅支持订单；是否查询当前数据？'
    elif 'inventory' not in resources and any(params.get(k) for k in ('product_ids', 'sku_ids')):
        unsupported = '当前商品目录查询支持商品状态或seller_skus筛选，product_ids/sku_ids仅用于库存查询'
    elif 'orders' not in resources and params.get('order_status'):
        unsupported = '订单状态筛选仅用于订单查询'
    elif 'products' not in resources and params.get('product_status'):
        unsupported = '商品状态筛选目前仅用于商品目录查询'
    elif not any(r in resources for r in ('products', 'inventory')) and params.get('seller_skus'):
        unsupported = 'seller_skus筛选目前仅用于商品或库存查询'
    if unsupported:
        # Do not retain unsupported filters that would trap a confirmation turn.
        context.pop('pending_shop', None)
        return unsupported, 'needs_input'
    # Validate all requested operations before issuing any business reads.
    requests = []
    try:
        for resource in resources:
            fields = {'shop_id': selected['id']}
            if resource == 'orders':
                fields.update({k: params[k] for k in ('relative_date', 'start_date', 'end_date', 'timezone', 'order_status') if params.get(k)})
                request = shop_read.OrdersQuery(**fields)
                shop_read.date_window(request, selected)
            elif resource == 'products':
                fields.update({k: params[k] for k in ('seller_skus',) if params.get(k)})
                if params.get('product_status'):
                    fields['status'] = params['product_status']
                request = shop_read.ProductsQuery(**fields)
            else:
                fields.update({k: params[k] for k in ('product_ids', 'sku_ids', 'seller_skus') if params.get(k)})
                request = shop_read.InventoryQuery(**fields)
            requests.append((resource, request))
    except ValidationError as exc:
        context['pending_shop'] = params
        errors = '; '.join(e['msg'].removeprefix('Value error, ') for e in exc.errors())
        return f"已确认店铺：{selected['name']}。请补充或更正参数：{errors}。", 'needs_input'
    except shop_api.ShopError as exc:
        context['pending_shop'] = params
        return f"已确认店铺：{selected['name']}。{exc}。", 'needs_input'
    sections, successes, errors = [], [], []
    for resource, request in requests:
        try:
            result = await shop_read.execute(workspace, resource, request, selected=selected)
            run['evidence'].extend(result['evidence'])
            sections.append(shop_read.render(result))
            successes.append({'operation': resource, 'complete': result['complete'], 'retrieved_count': result['retrieved_count']})
        except shop_api.ShopError as exc:
            errors.append({'operation': resource, 'error': str(exc)})
            sections.append(f"{resource} 查询失败：{exc}。没有用模拟数据代替。")
    run.update(data_verified=bool(successes), resolved_shop=shop_read.public_shop(selected), shop_results=successes, errors=errors)
    context.update(last_domain='shop', last_shop={k: v for k, v in params.items() if not k.startswith('reuse_')})
    context.pop('pending_shop', None)
    context.pop('pending_ads', None)
    return '\n\n'.join(sections), ('partial' if successes else 'failed') if errors else 'completed'
