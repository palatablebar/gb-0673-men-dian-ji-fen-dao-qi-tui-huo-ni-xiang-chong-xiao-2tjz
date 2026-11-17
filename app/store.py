import sqlite3, json, time
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS merges (id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, primary_account TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待对账', settled_balance INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

# ---- 合并记录 ----
# 状态：待对账 → 已生效；待对账 → 切换失败；暂缓合并/切换失败可重试回到待对账。
def _merge_row(con, merge_id):
    return con.execute('SELECT * FROM merges WHERE id=?',(merge_id,)).fetchone()

def _merge_view(row):
    if not row: return None
    result=json.loads(row['result'])
    result.update(revision=row['revision'],status=row['status'],settled_balance=row['settled_balance'],
                  created_at=row['created_at'],updated_at=row['updated_at'])
    return result

def create_merge(path, result):
    """按指纹幂等登记合并。重复提交返回原记录，不重算、不新增余额。"""
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO merges(id,fingerprint,primary_account,result,revision,status,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)',
                    (result['merge_id'],result['fingerprint'],result['primary'],json.dumps(result,ensure_ascii=False),result['status'],now,now))
        row=con.execute('SELECT * FROM merges WHERE fingerprint=?',(result['fingerprint'],)).fetchone()
    return _merge_view(row)

def get_merge(path, merge_id):
    with connect(path) as con: row=_merge_row(con,merge_id)
    if not row: raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
    return _merge_view(row)

def list_merges(path):
    with connect(path) as con:
        rows=con.execute('SELECT * FROM merges ORDER BY rowid DESC LIMIT 100').fetchall()
    return [_merge_view(row) for row in rows]

def active_groups(path, exclude=None):
    """参与余额归属的合并：待对账与已生效。暂缓/失败不占用账户归属。"""
    with connect(path) as con:
        rows=con.execute("SELECT * FROM merges WHERE status IN ('待对账','已生效')").fetchall()
    groups=[]
    for row in rows:
        view=_merge_view(row)
        if exclude and view['merge_id']==exclude: continue
        groups.append(view)
    return groups

def _update_merge(con, merge_id, revision, status, payload):
    cur=con.execute('UPDATE merges SET result=?,revision=revision+1,status=?,settled_balance=?,updated_at=? WHERE id=? AND revision=?',
                    (json.dumps(payload,ensure_ascii=False),status,payload.get('settled_balance'),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),merge_id,revision))
    if cur.rowcount!=1:
        if not _merge_row(con,merge_id): raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
        raise BusinessError('STALE_VERSION','合并版本已更新，请刷新',409)

def settle_merge(path, merge_id, revision, fresh=None):
    with connect(path) as con:
        row=_merge_row(con,merge_id)
        if not row: raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
        payload=json.loads(row['result'])
        if row['status']=='已生效':
            # 重复确认不新增余额、不推进版本。
            return _merge_view(row)
        if row['revision']!=revision: raise BusinessError('STALE_VERSION','合并版本已更新，请刷新',409)
        if row['status']!='待对账':
            raise BusinessError('MERGE_NOT_PENDING','仅待对账的合并可以确认生效',409)
        if not payload.get('reconciliation',{}).get('balanced'):
            raise BusinessError('MERGE_UNBALANCED','合并组积分不守恒，不能确认生效',409)
        if fresh is not None:
            # 以本次重算的余额落账，不信任可能被改写的存档数值。
            if fresh.get('fingerprint')!=payload.get('fingerprint'):
                raise BusinessError('MERGE_STALE','合并依据已变化，请重新合并',409)
            payload={**fresh,'merge_id':row['id'],'fingerprint':row['fingerprint']}
        payload['usable_balance']=payload['merged_balance']
        payload['settled_balance']=payload['merged_balance']
        payload['settled_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        _update_merge(con,merge_id,revision,'已生效',payload)
        return _merge_view(_merge_row(con,merge_id))

def fail_merge(path, merge_id, revision, reason=''):
    with connect(path) as con:
        row=_merge_row(con,merge_id)
        if not row: raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
        payload=json.loads(row['result'])
        if row['status']=='切换失败':
            return _merge_view(row)
        if row['revision']!=revision: raise BusinessError('STALE_VERSION','合并版本已更新，请刷新',409)
        if row['status']!='待对账':
            raise BusinessError('MERGE_NOT_PENDING','仅待对账的合并可以回退',409)
        # 切换失败：旧版余额继续生效，合并结果不落账。
        payload['usable_balance']=payload['rollback_balance']
        payload['settled_balance']=None
        payload['failure_reason']=reason or '切换失败，保留旧版余额'
        payload['failed_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        _update_merge(con,merge_id,revision,'切换失败',payload)
        return _merge_view(_merge_row(con,merge_id))

def retry_merge(path, merge_id, revision, result):
    """用最新重算结果刷新暂缓/失败的合并；指纹不变，余额按重算结果，不叠加。"""
    with connect(path) as con:
        row=_merge_row(con,merge_id)
        if not row: raise BusinessError('MERGE_NOT_FOUND','合并记录不存在',404)
        if row['revision']!=revision: raise BusinessError('STALE_VERSION','合并版本已更新，请刷新',409)
        if row['status']=='已生效':
            raise BusinessError('MERGE_ALREADY_SETTLED','已生效的合并不能再重试',409)
        payload={**result,'merge_id':row['id'],'fingerprint':row['fingerprint'],
                 'settled_balance':None,'usable_balance':result['merged_balance'] if result['verdict']=='MERGE' else result['primary_balance_before']}
        _update_merge(con,merge_id,revision,payload['status'],payload)
        return _merge_view(_merge_row(con,merge_id))
