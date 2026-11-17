from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def _merge_context(db, request, state):
    from app.modules.merge import rules
    merge_id=request.query_params.get('merge_id')
    if merge_id:
        try:
            result=store.get_merge(db,merge_id)
        except Exception as error:
            return {'merge_result':None,'merge_error':str(error),'merge_form':{}}
        return {'merge_result':result,'merge_form':{'primary':result['primary'],'accounts':','.join(result['accounts'])}}
    primary=request.query_params.get('primary')
    accounts=request.query_params.get('accounts')
    if primary is None:
        primary,accounts=rules.default_accounts(state)
    else:
        accounts=[a.strip() for a in (accounts or '').replace('，', ',').split(',') if a.strip()]
    form={'primary':primary,'accounts':','.join(accounts)}
    try:
        result=rules.build(state,primary,accounts,run_id=state['manifest']['run_id'],
                           active_groups=store.active_groups(db,exclude=rules.merge_id_for(state['manifest']['run_id'],primary,accounts)))
    except Exception as error:  # 页面预览不因入参错误中断导航
        return {'merge_result':None,'merge_error':str(error),'merge_form':form}
    # 预览只读：已登记的同一合并显示持久化状态，未登记的只显示裁决，由操作按钮负责登记。
    try:
        saved=store.get_merge(db,result['merge_id'])
    except Exception:
        saved=None
    return {'merge_result':saved or result,'merge_error':None,'merge_form':form}

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    context={'meta':META,'key':key,'state':state,'view':state['views'].get(key) or {'rows':[],'metrics':{}}}
    if key=='merge':
        context.update(_merge_context(DB_PATH,request,state))
    return templates.TemplateResponse(request=request,name=f'{key}.html',context=context)
