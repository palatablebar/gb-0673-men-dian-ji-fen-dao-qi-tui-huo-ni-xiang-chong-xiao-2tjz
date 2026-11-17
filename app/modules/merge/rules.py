"""账户合并：把主账户与待合并账户作为一次归属裁决处理。

设计约束（与业务约定一一对应）：
- 只做归属叠加投影，不改写任何原始事件、批次或隔离记录，原账户依据永远可查；
- 合并前用当前余额、历史重放和对账结果共同裁决，历史结论会变就暂缓；
- 同一批次/消费/退款按编号只计一次，重复执行复用同一合并编号，不新增余额；
- 合并后余额当天可用，但对账确认后才正式生效；切换失败回退旧版余额；
- 原始归属从不被覆盖，合并记录额外保存合并前快照，可完整回溯。
"""
import hashlib
import json
from datetime import datetime, timezone

from app.config import RULE_VERSION
from app.errors import BusinessError
from app.modules.consumption.rules import replay

# 只有消费/退款阶段可能因“归属不同”而改判的隔离原因；其余失败与归属无关。
CANDIDATE_CODES = {'INSUFFICIENT_POINTS', 'REFUND_REFERENCE', 'REFUND_CAP'}
STATUS_PENDING = '待对账'
STATUS_DEFERRED = '暂缓合并'
STATUS_SETTLED = '已生效'
STATUS_FAILED = '切换失败'
ATTRIBUTION_POLICY = '保留原归属（叠加投影，不改写历史）'


def normalize_accounts(primary, accounts):
    if not isinstance(primary, str) or not primary.strip():
        raise BusinessError('BAD_MERGE_INPUT', '主账户不能为空')
    if isinstance(accounts, str):
        accounts = accounts.replace('，', ',').split(',')
    if not isinstance(accounts, (list, tuple)):
        raise BusinessError('BAD_MERGE_INPUT', '待合并账户须为账户号列表')
    cleaned = []
    for value in accounts:
        if not isinstance(value, str) or not value.strip():
            raise BusinessError('BAD_MERGE_INPUT', '待合并账户不能为空')
        cleaned.append(value.strip())
    primary = primary.strip()
    aliases = sorted({value for value in cleaned if value != primary})
    if not aliases:
        raise BusinessError('BAD_MERGE_INPUT', '至少提交一个不同于主账户的待合并账户')
    return primary, aliases


def identity_fingerprint(run_id, primary, aliases):
    identity = {'run_id': run_id, 'primary': primary, 'accounts': list(aliases), 'rule_version': RULE_VERSION}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def merge_id_for(run_id, primary, aliases):
    return 'M' + identity_fingerprint(run_id, primary, aliases)[:15]


def _account_data(state):
    data = {}

    def bucket(member):
        return data.setdefault(member, {'member': member, 'lots': [], 'spends': [], 'refunds': [],
                                        'events': [], 'quarantined': []})

    ledger = state.get('point_ledger') or {}
    for lot in ledger.get('lots', []):
        bucket(lot['member'])['lots'].append(lot)
    for spend in ledger.get('spends', []):
        bucket(spend['member'])['spends'].append(spend)
    for refund in ledger.get('refunds', []):
        bucket(refund['member'])['refunds'].append(refund)
    for row in state.get('records', []):
        bucket(row['member'])['events'].append(row)
    for item in state.get('quarantine', []):
        raw = item.get('raw') or {}
        member = (raw.get('member') or '').strip()
        if member:
            bucket(member)['quarantined'].append(item)
    return data


def _signature(kind, item):
    if kind == 'lots':
        # 同编号批次必须连剩余余额都一致，否则视为冲突而不是静默取一边。
        return (item.get('issued'), item.get('remaining'), item.get('expires'), item.get('at'))
    if kind == 'spends':
        return (item.get('points'), item.get('at'))
    return (item.get('ref'), item.get('points'))


def _summarise(state, group):
    data = _account_data(state)
    group_set = set(group)
    seen = {'lots': {}, 'spends': {}, 'refunds': {}}
    picked = {'lots': [], 'spends': [], 'refunds': []}
    duplicates = 0
    conflicts = []
    for account in group:
        info = data.get(account) or {}
        for kind in ('lots', 'spends', 'refunds'):
            for item in info.get(kind, []):
                held = seen[kind].get(item['id'])
                if held is not None:
                    if _signature(kind, held[1]) == _signature(kind, item):
                        duplicates += 1
                    else:
                        conflicts.append(item['id'])
                    continue
                seen[kind][item['id']] = (account, item)
                picked[kind].append(item)

    issued = sum(lot['issued'] for lot in picked['lots'])
    remaining = sum(lot['remaining'] for lot in picked['lots'])
    spent = sum(item['points'] for item in picked['spends'])
    refunded = sum(item['points'] for item in picked['refunds'])
    natural_expired = sum(item['points'] for item in state['point_ledger']['ledger']
                          if item['action'] == 'EXPIRE' and item['member'] in group_set)
    expired_refund = sum(item.get('expired_refund', 0) for item in picked['refunds'])
    expired = natural_expired + expired_refund
    balanced = issued == remaining + spent - refunded + expired
    return {'issued': issued, 'remaining': remaining, 'spent': spent, 'refunded': refunded,
            'expired': expired, 'balanced': balanced,
            'lots': len(picked['lots']), 'spends': len(picked['spends']),
            'refunds': len(picked['refunds']), 'duplicate_events_skipped': duplicates,
            'conflicts': sorted(set(conflicts))}


def _account_detail(state, group):
    data = _account_data(state)
    seen = {'lots': set(), 'spends': set(), 'refunds': set()}
    rows = []
    for account in group:
        info = data.get(account)
        if info is None:
            rows.append({'account': account, 'known': False, 'remaining': 0, 'issued': 0,
                         'spent': 0, 'refunded': 0, 'events': 0, 'quarantined': 0, 'lots': '',
                         'counted_remaining': 0})
            continue
        counted = 0
        for lot in info['lots']:
            if lot['id'] in seen['lots']:
                continue  # 同一批次已在更靠前的账户计过，不再重复计入合并余额
            seen['lots'].add(lot['id'])
            counted += lot['remaining']
        for kind in ('spends', 'refunds'):
            ids = {item['id'] for item in info[kind]}
            seen[kind] |= ids
        rows.append({'account': account, 'known': True,
                     'remaining': sum(lot['remaining'] for lot in info['lots']),
                     'issued': sum(lot['issued'] for lot in info['lots']),
                     'spent': sum(item['points'] for item in info['spends']),
                     'refunded': sum(item['points'] for item in info['refunds']),
                     'events': len(info['events']), 'quarantined': len(info['quarantined']),
                     'lots': ' '.join(lot['id'] for lot in info['lots']),
                     'counted_remaining': counted})
    return rows


def _evidence(state, group):
    data = _account_data(state)
    rows = []
    for account in group:
        info = data.get(account) or {}
        for row in info.get('events', []):
            rows.append({'account': account, 'id': row.get('id'), 'type': row.get('type'),
                         'points': row.get('points'), 'at': row.get('at'),
                         'line': row.get('_line'), 'state': '采纳'})
        for item in info.get('quarantined', []):
            raw = item.get('raw') or {}
            rows.append({'account': account, 'id': item.get('id'), 'type': raw.get('type'),
                         'points': raw.get('points'), 'at': raw.get('at'),
                         'line': item.get('line'), 'state': item.get('code')})
    rows.sort(key=lambda row: (row['account'], row.get('line') or 0, row.get('id') or ''))
    return rows


def _candidate_rows(state, group):
    rows = []
    for item in state.get('quarantine', []):
        if item.get('code') not in CANDIDATE_CODES:
            continue
        raw = dict(item.get('raw') or {})
        member = (raw.get('member') or '').strip()
        if member not in group:
            continue
        raw['member'] = member
        try:
            raw['points'] = int(raw.get('points'))
        except (TypeError, ValueError):
            continue
        raw.setdefault('_line', item.get('line', 0))
        rows.append((item, raw))
    return rows


def _history_check(state, group, primary):
    """模拟“直接改归属”重放一次，若历史结论会变，则不允许合并。"""
    accepted = [dict(row) for row in state['records'] if row['member'] in group]
    candidates = _candidate_rows(state, set(group))
    lots, seen = [], set()
    for lot in state['lots']:
        if lot['member'] in group and lot['id'] not in seen:
            seen.add(lot['id'])
            lots.append(dict(lot))

    def remap(row):
        return {**row, 'member': primary}

    merged = replay([remap(row) for row in accepted] + [remap(raw) for _, raw in candidates],
                    [remap(lot) for lot in lots], state['context']['as_of'])
    success = {row['id'] for row in merged['spends']} | {row['id'] for row in merged['refunds']}
    # 只对比消费/退款结论；发放事件不会出现在成功集合里，不能当历史丢失。
    accepted_ids = {row['id'] for row in accepted if row['type'] in ('SPEND', 'REFUND')}
    flips = sorted(success - accepted_ids)
    lost = sorted(accepted_ids - success)
    # 叠加投影与直接改归属必须得到同一个余额与同一笔到期；按批次编号去重后再比。
    overlay_remaining = 0
    counted = set()
    for lot in state['point_ledger']['lots']:
        if lot['member'] in group and lot['id'] not in counted:
            counted.add(lot['id'])
            overlay_remaining += lot['remaining']
    merged_remaining = sum(lot['remaining'] for lot in merged['lots'])
    overlay_expired = sum(item['points'] for item in state['point_ledger']['ledger']
                          if item['action'] == 'EXPIRE' and item['member'] in group) + \
                      sum(refund.get('expired_refund', 0) for refund in state['point_ledger']['refunds']
                          if refund['member'] in group)
    merged_expired = merged['expired'] + sum(refund.get('expired_refund', 0) for refund in merged['refunds'])
    return {'stable': not flips and not lost and merged_remaining == overlay_remaining and merged_expired == overlay_expired,
            'flips': flips, 'lost': lost,
            'overlay_remaining': overlay_remaining, 'merged_remaining': merged_remaining,
            'overlay_expired': overlay_expired, 'merged_expired': merged_expired,
            'candidate_refs': {event['id']: raw.get('ref') for event, raw in candidates}}


def _history_detail(history):
    parts = []
    for event_id in history['flips']:
        parts.append(f'{event_id} 按合并归属重放会由隔离改为成功')
    for event_id in history['lost']:
        parts.append(f'{event_id} 按合并归属重放会由成功改为失败')
    if history['merged_remaining'] != history['overlay_remaining']:
        parts.append(f'合并归属重放余额 {history["merged_remaining"]} 与原账户余额合计 '
                     f'{history["overlay_remaining"]} 不一致，到期归属会被改写')
    if history['merged_expired'] != history['overlay_expired']:
        parts.append(f'合并归属重放到期 {history["merged_expired"]} 与原账户到期合计 '
                     f'{history["overlay_expired"]} 不一致，到期依据不能改写')
    return '；'.join(parts)


def _reasons(state, group, summary, history, active_groups, run_id):
    reasons = []
    unknown = [row['account'] for row in _account_detail(state, group) if not row['known']]
    if unknown:
        reasons.append({'code': 'UNKNOWN_ACCOUNT',
                        'detail': '账户 ' + '、'.join(unknown) + ' 没有任何事件、批次或隔离依据，无法核对历史'})
    group_set = set(group)
    for active in active_groups:
        if active.get('run_id') != run_id:
            continue  # 不同批次是各自独立的依据，不能跨批次占用账户
        overlap = sorted(group_set & set(active['group']))
        if overlap:
            reasons.append({'code': 'MERGE_ALREADY_SETTLED' if active['status'] == STATUS_SETTLED else 'MERGE_OVERLAP',
                            'detail': '账户 ' + '、'.join(overlap) + ' 已属于合并 ' + active['merge_id'] +
                                      '（' + active['status'] + '），同一余额不能重复归属'})
            break
    if summary['conflicts']:
        reasons.append({'code': 'MERGE_ID_CONFLICT',
                        'detail': '编号 ' + '、'.join(summary['conflicts']) + ' 在不同账户原值不同，不能合并计一次'})
    if history['flips'] or history['lost'] or history['merged_remaining'] != history['overlay_remaining'] \
            or history['merged_expired'] != history['overlay_expired']:
        reasons.append({'code': 'HISTORY_UNSTABLE', 'detail': _history_detail(history),
                        'events': history['flips'] + history['lost']})
    if not summary['balanced']:
        reasons.append({'code': 'RECONCILE_FAILED',
                        'detail': f'合并组积分不守恒：发放{summary["issued"]} ≠ 可用{summary["remaining"]}'
                                  f'+消费{summary["spent"]}-退款{summary["refunded"]}+到期{summary["expired"]}'})
    if not state.get('balance', {}).get('row_balanced'):
        reasons.append({'code': 'RECONCILE_FAILED', 'detail': '原始行数未守恒，不能作为合并依据'})
    if state.get('refund_issues'):
        reasons.append({'code': 'RECONCILE_FAILED',
                        'detail': '退货回拨审计未通过：' + '、'.join(state['refund_issues'])})
    return reasons


def build(state, primary, accounts, run_id=None, active_groups=()):
    primary, aliases = normalize_accounts(primary, accounts)
    group = [primary] + aliases
    run_id = run_id or state['manifest']['run_id']

    detail = _account_detail(state, group)
    summary = _summarise(state, group)
    history = _history_check(state, group, primary)
    reasons = _reasons(state, group, summary, history, active_groups, run_id)
    previous = next((row['remaining'] for row in detail if row['account'] == primary), 0)
    verdict = 'MERGE' if not reasons else 'DEFER'
    status = STATUS_PENDING if verdict == 'MERGE' else STATUS_DEFERRED
    merged_balance = summary['remaining']
    # 合并后归属视图：裁决通过时主账户取得合并余额，待合并账户归零；历史仍按原账户保留。
    for row in detail:
        if verdict != 'MERGE':
            row['effective_remaining'] = row['remaining']
        else:
            row['effective_remaining'] = merged_balance if row['account'] == primary else 0

    fingerprint = identity_fingerprint(run_id, primary, aliases)

    return {
        'merge_id': merge_id_for(run_id, primary, aliases),
        'fingerprint': fingerprint,
        'run_id': run_id,
        'primary': primary,
        'accounts': aliases,
        'group': group,
        'verdict': verdict,
        'status': status,
        'reasons': reasons,
        'attribution_policy': ATTRIBUTION_POLICY,
        'reattribution_performed': False,
        'primary_balance_before': previous,
        'merged_balance': merged_balance,
        'usable_balance': merged_balance if verdict == 'MERGE' else previous,
        'settled_balance': None,
        'delta': merged_balance - previous if verdict == 'MERGE' else 0,
        'rollback_balance': previous,
        'reconciliation': {**summary, 'row_balanced': bool(state.get('balance', {}).get('row_balanced'))},
        'accounts_detail': detail,
        'evidence': _evidence(state, group),
        'snapshot': [{'account': row['account'], 'remaining': row['remaining'], 'lots': row['lots']}
                     for row in detail],
        'history': history,
        'rule_version': RULE_VERSION,
        'as_of': state['context']['as_of'],
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }


def apply(state):
    """合并是独立操作，不进入每日重算的账务结果；这里只登记视图供导航渲染。"""
    state['views']['merge'] = {'title': '账户合并', 'rows': [], 'metrics': {}}


def default_accounts(state):
    """默认演示组合：00001 为主账户，00004 为待合并账户；数据缺号时退回到前两个会员。"""
    members = []
    for row in state.get('point_ledger', {}).get('lots', []):
        if row['member'] not in members:
            members.append(row['member'])
    if '00001' in members and '00004' in members:
        return '00001', ['00004']
    if len(members) >= 2:
        return members[0], [members[1]]
    return (members[0] if members else '00001'), []
