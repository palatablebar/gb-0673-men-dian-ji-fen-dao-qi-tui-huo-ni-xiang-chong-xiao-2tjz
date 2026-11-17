from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
from app.modules.merge import rules
from copy import deepcopy

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'merge.sqlite'))
    return TestClient(main.app)

def seed(c,dataset='dirty'):
    return c.post('/api/runs',json={'dataset':dataset}).json()['manifest']['run_id']

def create(c,run_id,primary,accounts):
    return c.post('/api/merge',json={'run_id':run_id,'primary':primary,'accounts':accounts}).json()

def test_dirty_default_merges_primary_and_pending(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    assert result['verdict']=='MERGE'
    assert result['status']=='待对账'
    assert result['primary_balance_before']==30
    assert result['merged_balance']==100
    assert result['usable_balance']==100          # 当天即可用合并后的积分
    assert result['settled_balance'] is None      # 但未对账确认前不正式生效
    assert result['reconciliation']['balanced'] and result['reconciliation']['row_balanced']

def test_history_unstable_defers_and_keeps_old_balance(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    # 00002 的 BAD4 退款引用 00001 的消费，直接改归属会让这条历史隔离翻案。
    result=create(c,run_id,'00001',['00002'])
    assert result['verdict']=='DEFER' and result['status']=='暂缓合并'
    assert [r['code'] for r in result['reasons']]==['HISTORY_UNSTABLE']
    assert 'BAD4' in result['reasons'][0]['detail']
    assert result['usable_balance']==30 and result['delta']==0
    assert c.post('/api/merges/'+result['merge_id']+'/settle',json={'revision':result['revision']}).status_code==409

def test_unknown_account_defers(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['99999'])
    assert result['verdict']=='DEFER'
    assert [r['code'] for r in result['reasons']]==['UNKNOWN_ACCOUNT']

def test_effective_attribution_after_merge(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    detail={row['account']:row for row in result['accounts_detail']}
    assert detail['00001']['effective_remaining']==100   # 主账户当天拿到合并余额
    assert detail['00004']['effective_remaining']==0
    assert detail['00001']['remaining']==30 and detail['00004']['remaining']==70  # 原账户依据不变
    deferred=create(c,run_id,'00001',['00002'])
    detail={row['account']:row for row in deferred['accounts_detail']}
    assert detail['00001']['effective_remaining']==30 and detail['00002']['effective_remaining']==70

def test_evidence_keeps_original_accounts(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    assert result['attribution_policy']=='保留原归属（叠加投影，不改写历史）'
    assert result['reattribution_performed'] is False
    accounts={row['account'] for row in result['evidence']}
    assert {'00001','00004'}<=accounts
    assert any(row['id']=='E1' and row['account']=='00001' for row in result['evidence'])
    assert any(row['id']=='E5' and row['account']=='00004' for row in result['evidence'])
    detail={row['account']:row for row in result['accounts_detail']}
    assert detail['00001']['remaining']==30 and detail['00004']['remaining']==70
    # 原始批次与事件不改写
    state=c.get('/api/runs/'+run_id).json()
    assert {lot['member'] for lot in state['lots']} >= {'00001','00004'}

def test_repeat_create_does_not_add_balance(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    first=create(c,run_id,'00001',['00004'])
    second=create(c,run_id,'00001',['00004'])
    assert second['merge_id']==first['merge_id']
    assert second['merged_balance']==first['merged_balance']
    assert len(c.get('/api/merges').json())==1

def test_settle_is_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    settled=c.post('/api/merges/'+result['merge_id']+'/settle',json={'revision':result['revision']}).json()
    assert settled['status']=='已生效' and settled['settled_balance']==100
    again=c.post('/api/merges/'+result['merge_id']+'/settle',json={'revision':result['revision']}).json()
    assert again['revision']==settled['revision'] and again['settled_balance']==100
    assert create(c,run_id,'00001',['00004'])['status']=='已生效'

def test_switch_failure_restores_old_balance(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00002',['00003'])
    failed=c.post('/api/merges/'+result['merge_id']+'/fail',json={'revision':result['revision'],'reason':'网关超时'}).json()
    assert failed['status']=='切换失败'
    assert failed['usable_balance']==result['rollback_balance']==70
    assert failed['settled_balance'] is None
    assert c.post('/api/merges/'+result['merge_id']+'/settle',json={'revision':failed['revision']}).status_code==409
    retried=c.post('/api/merges/'+result['merge_id']+'/retry',json={'revision':failed['revision']}).json()
    assert retried['status']=='待对账' and retried['merged_balance']==140

def test_account_cannot_be_merged_twice(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    first=create(c,run_id,'00001',['00004'])
    c.post('/api/merges/'+first['merge_id']+'/settle',json={'revision':first['revision']})
    second=create(c,run_id,'00004',['00005'])
    assert second['verdict']=='DEFER'
    assert second['reasons'][0]['code']=='MERGE_ALREADY_SETTLED'

def test_same_accounts_in_other_run_are_independent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);dirty=seed(c);clean=seed(c,'clean')
    first=create(c,dirty,'00001',['00004'])
    c.post('/api/merges/'+first['merge_id']+'/settle',json={'revision':first['revision']})
    other=create(c,clean,'00001',['00004'])
    assert other['verdict']=='MERGE' and other['status']=='待对账'

def test_stale_revision_conflicts(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    assert c.post('/api/merges/'+result['merge_id']+'/settle',json={'revision':result['revision']+5}).status_code==409

def test_missing_merge_404(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/merges/none').status_code==404

def test_merge_page_shows_result(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    html=c.get('/merge?run_id='+run_id+'&merge_id='+result['merge_id']).text
    assert result['merge_id'] in html
    assert '100' in html and '原账户依据' in html and '对账' in html

def test_page_reload_does_not_create_new_merge(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    for _ in range(3):
        assert c.get('/merge?run_id='+run_id).status_code==200
    # 只预览不落库，重复打开页面不产生合并记录。
    assert c.get('/api/merges').json()==[]
    result=create(c,run_id,'00001',['00004'])
    for _ in range(3):
        assert c.get('/merge?run_id='+run_id+'&merge_id='+result['merge_id']).status_code==200
    assert len(c.get('/api/merges').json())==1

def test_form_submit_settles_and_redirects(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    result=create(c,run_id,'00001',['00004'])
    response=c.post('/api/merges/'+result['merge_id']+'/settle',
                    data={'revision':result['revision'],'run_id':run_id,'primary':'00001','accounts':'00004'},
                    follow_redirects=False)
    assert response.status_code==303
    assert result['merge_id'] in response.headers['location']
    assert '已生效' in c.get(response.headers['location']).text

def test_form_submit_can_register_preview_without_api(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);run_id=seed(c)
    # 未先调用 /api/merge 时，页面上的操作按钮按同一依据幂等登记后继续。
    preview=c.get('/merge?run_id='+run_id+'&primary=00001&accounts=00004')
    assert preview.status_code==200
    merge_id=preview.text.split('action="/api/merges/')[1].split('/settle')[0]
    response=c.post('/api/merges/'+merge_id+'/settle',
                    data={'revision':1,'run_id':run_id,'primary':'00001','accounts':'00004'},
                    follow_redirects=False)
    assert response.status_code==303
    assert c.get('/api/merges/'+merge_id).json()['status']=='已生效'

def test_duplicate_lot_counted_once(clean):
    state=deepcopy(clean)
    # 人为让两个账户持有同一批次编号，合并只能计一次。
    duplicate=deepcopy(next(x for x in state['point_ledger']['lots'] if x['member']=='00001' and x['id']=='E2'))
    duplicate['member']='00004'
    state['point_ledger']['lots'].append(duplicate)
    result=rules.build(state,'00001',['00004'])
    assert result['reconciliation']['duplicate_events_skipped']==1
    detail={row['account']:row for row in result['accounts_detail']}
    # 账户明细仍按原账户各自展示，重复批次在 00004 名下也照实列出。
    assert detail['00004']['remaining']==70+30
    assert detail['00004']['counted_remaining']==70   # 但合并只计 00004 自己名下未被重复的那部分
    # 合并余额只计一次：00001 的 30 加 00004 自己的 70，重复的 E2 不再加。
    assert result['merged_balance']==100
    assert result['reconciliation']['lots']==len({x['id'] for x in state['point_ledger']['lots'] if x['member'] in {'00001','00004'}})

def test_conflicting_lot_id_refused(clean):
    state=deepcopy(clean)
    duplicate=deepcopy(next(x for x in state['point_ledger']['lots'] if x['member']=='00001'))
    duplicate['member']='00004';duplicate['issued']+=1;duplicate['remaining']+=1
    state['point_ledger']['lots'].append(duplicate)
    result=rules.build(state,'00001',['00004'])
    assert [r['code'] for r in result['reasons']][0]=='MERGE_ID_CONFLICT'

def test_primary_in_accounts_not_double_counted(clean):
    result=rules.build(clean,'00001',['00001','00004'])
    assert result['accounts']==['00004']
