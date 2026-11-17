from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
import json
from app.engine import evaluate
from app.errors import BusinessError
from app.web import render
from app import store
from app.modules.merge import rules

router=APIRouter()

def _db():
    from app.main import DB_PATH
    return DB_PATH

def _state_from(payload):
    """取当前批次：优先复用已存在的 run_id，否则按入参重算并登记。"""
    run_id=payload.get('run_id')
    if run_id:
        return store.get(_db(),run_id)
    return store.save(_db(),evaluate(payload))

async def _body(request:Request):
    """同时接受 JSON 与页面表单；表单提交后跳回合并详情。

    表单按 urlencoded 手工解析，避免为一个操作按钮引入额外依赖。
    """
    content=request.headers.get('content-type','')
    raw=await request.body()
    if content.startswith('application/x-www-form-urlencoded'):
        from urllib.parse import parse_qs
        parsed=parse_qs(raw.decode('utf-8','replace'),keep_blank_values=True)
        return {key:values[-1] for key,values in parsed.items()},True
    try:
        data=json.loads(raw or b'{}')
    except ValueError:
        data={}
    return (data if isinstance(data,dict) else {}),False

def _accounts(value):
    if value is None: return None
    if isinstance(value,str): return [v.strip() for v in value.replace('，',',').split(',') if v.strip()]
    return value

def _revision(payload,current):
    revision=payload.get('revision',current['revision'])
    try: revision=int(revision)
    except (TypeError,ValueError): raise BusinessError('BAD_REVISION','版本号须为正整数')
    if revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return revision

@router.get('/merge')
def page(request:Request):
    return render(request,'merge')

@router.post('/api/merge')
async def calculate(request:Request):
    db=_db();payload,form=await _body(request)
    state=_state_from(payload)
    primary=payload.get('primary')
    accounts=_accounts(payload.get('accounts'))
    if primary is None or accounts is None:
        default_primary,default_accounts=rules.default_accounts(state)
        primary=primary or default_primary
        accounts=accounts if accounts is not None else default_accounts
    result=rules.build(state,primary,accounts,run_id=state['manifest']['run_id'],
                       active_groups=store.active_groups(db,exclude=rules.merge_id_for(state['manifest']['run_id'],primary,accounts)))
    saved=store.create_merge(db,result)
    if form:
        return RedirectResponse(f"/merge?run_id={saved['run_id']}&merge_id={saved['merge_id']}",status_code=303)
    return saved

@router.get('/api/merges')
def merges():
    return store.list_merges(_db())

@router.get('/api/merges/{merge_id}')
def merge_detail(merge_id:str):
    return store.get_merge(_db(),merge_id)

def _fresh(db,current):
    """结算/重试前按当前批次重算，杜绝用过期依据确认。"""
    state=store.get(db,current['run_id'])
    return rules.build(state,current['primary'],current['accounts'],run_id=current['run_id'],
                       active_groups=store.active_groups(db,exclude=current['merge_id']))

def _ensure(db, merge_id, payload):
    """页面预览不写库；点操作按钮时才按同一依据幂等登记合并记录。"""
    try:
        return store.get_merge(db, merge_id)
    except BusinessError as error:
        if error.code!='MERGE_NOT_FOUND': raise
    run_id=payload.get('run_id');primary=payload.get('primary');accounts=_accounts(payload.get('accounts'))
    if not run_id or not primary or accounts is None:
        raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
    state=store.get(db,run_id)
    result=rules.build(state,primary,accounts,run_id=run_id,
                       active_groups=store.active_groups(db,exclude=merge_id))
    if result['merge_id']!=merge_id:
        raise BusinessError('MERGE_NOT_FOUND','合并记录与提交依据不一致',404)
    return store.create_merge(db,result)

@router.post('/api/merges/{merge_id}/settle')
async def settle(merge_id:str,request:Request):
    db=_db();payload,form=await _body(request)
    current=_ensure(db,merge_id,payload)
    revision=_revision(payload,current)
    if current['status']=='已生效':
        result=current  # 重复确认不新增余额、不推进版本
    elif current['status']!='待对账':
        raise BusinessError('MERGE_NOT_PENDING','仅待对账的合并可以确认生效',409)
    else:
        fresh=_fresh(db,current)
        if fresh['fingerprint']!=current['fingerprint']:
            raise BusinessError('MERGE_STALE','合并依据已变化，请重新合并',409)
        if fresh['verdict']!='MERGE':
            raise BusinessError('MERGE_UNBALANCED','对账未通过：'+'；'.join(r['detail'] for r in fresh['reasons']),409)
        result=store.settle_merge(db,merge_id,revision,fresh)
    if form:
        return RedirectResponse(f"/merge?run_id={current['run_id']}&merge_id={result['merge_id']}",status_code=303)
    return result

@router.post('/api/merges/{merge_id}/fail')
async def fail(merge_id:str,request:Request):
    db=_db();payload,form=await _body(request)
    current=_ensure(db,merge_id,payload)
    revision=_revision(payload,current)
    result=store.fail_merge(db,merge_id,revision,payload.get('reason',''))
    if form:
        return RedirectResponse(f"/merge?run_id={current['run_id']}&merge_id={result['merge_id']}",status_code=303)
    return result

@router.post('/api/merges/{merge_id}/retry')
async def retry(merge_id:str,request:Request):
    db=_db();payload,form=await _body(request)
    current=_ensure(db,merge_id,payload)
    revision=_revision(payload,current)
    if current['status']=='已生效':
        raise BusinessError('MERGE_ALREADY_SETTLED','已生效的合并不能再重试',409)
    result=store.retry_merge(db,merge_id,revision,_fresh(db,current))
    if form:
        return RedirectResponse(f"/merge?run_id={current['run_id']}&merge_id={result['merge_id']}",status_code=303)
    return result
