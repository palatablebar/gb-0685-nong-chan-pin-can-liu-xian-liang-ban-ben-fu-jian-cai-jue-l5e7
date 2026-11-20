"""复验撤回裁决规则（纯函数，不触碰存储层）。

撤回后以被撤回复验所替代的原始报告作为当前依据：
样本判定、批次归级与封存口径统一跟随原始报告重算；
被撤回的复验不删除，仅以撤回证据留痕。
"""
from copy import deepcopy
from hashlib import sha256

from app.errors import DomainError
from app.modules.m5_calculate.rules import select_reports
from app.modules.m6_reconcile.compare import balance

ALLOWED_REASONS = {
    'LIMIT_VERSION': '依据的限量版本目录改版，复验依据失效',
    'OTHER': '实验室主动撤回',
}


def withdrawal_key(run_id, report_id):
    """同一撤回重跑的幂等键：运行+被撤回报告，不含时间与理由。"""
    return sha256('{}|{}'.format(run_id, report_id).encode('utf-8')).hexdigest()[:20]


def _snapshot(body):
    """抽取重算所需的运行快照，保证只在已归档结论上重放，不重新跑工作流。"""
    fields = ('measurements', 'quarantine', 'rows', 'input_count', 'blank_count',
              'header_count', 'duplicate_count', 'calculations', 'business',
              'reconcile', 'run_id')
    snap = {f: deepcopy(body[f]) for f in fields if f in body}
    return snap


def _recalc(snapshot):
    """在剔除被撤回复验后的快照上重算 M5 判定、业务账与 M6 批次归级。"""
    chosen, bad = select_reports(snapshot['measurements'])
    if bad:
        # 撤回仅剔除当前有效复验，链条其余部分必须仍然自洽。
        raise DomainError('REFERENCE', '撤回后复检链出现新的无效引用，已拒绝处理')
    calculations = [{
        'id': r['id'], 'line': r['_line'], 'sample_id': r['sample_id'],
        'batch': r['batch'], 'analyte': r['analyte'], 'method': r['method'],
        'standard': r['standard_id'], 'lower': r['lower'], 'upper': r['upper'],
        'upper_exclusive': r['upper_exclusive'], 'limit': r['limit'],
        'code': r['code'], 'replaces': r['relation'],
    } for r in chosen]
    snapshot['calculations'] = calculations
    snapshot['business'] = {
        'total': len(calculations),
        'pass': sum(r['code'] == 'PASS' for r in calculations),
        'fail': sum(r['code'] == 'FAIL' for r in calculations),
        'uncertain': sum(r['code'] == 'UNCERTAIN' for r in calculations),
        'unit': '检测项目',
    }
    snapshot['reconcile'] = balance(snapshot)
    return snapshot


def build_withdrawal(body, report_id, reason, withdrawn_at):
    """生成一份可归档的撤回处理结论（纯函数，不修改入参）。

    校验失败抛 DomainError；成功返回 (结论, 新运行体)。
    """
    reason = (reason or 'LIMIT_VERSION').strip()
    if reason not in ALLOWED_REASONS:
        raise DomainError('REASON', '撤回原因码无效')
    if not isinstance(report_id, str) or not report_id.strip():
        raise DomainError('REPORT', '请指定撤回的复验报告ID')
    report_id = report_id.strip()

    current = {c['id']: c for c in body.get('calculations', [])}
    target = current.get(report_id)
    # 撤回对象必须是当前裁决清单里替代原始报告的有效复验。
    if target is None or not target.get('replaces'):
        raise DomainError('REPORT', '该报告不是当前有效复验，不能撤回')

    prior = body.get('withdrawals', [])
    if any(w['report_id'] == report_id for w in prior):
        raise DomainError('DUPLICATE', '该复验已撤回，同一撤回不得重复登记', 409)

    snapshot = _snapshot(body)
    target_measurement = next((m for m in snapshot['measurements'] if m['id'] == report_id), None)
    if target_measurement is None:
        raise DomainError('REPORT', '撤回报告缺少采信证据')

    snapshot['measurements'] = [m for m in snapshot['measurements'] if m['id'] != report_id]
    snapshot = _recalc(snapshot)

    run_id = body['run_id']
    before = body['business']
    after = snapshot['business']
    restored_verdict = next(c['code'] for c in snapshot['calculations'] if c['id'] == target['replaces'])
    sealing_word = {'PASS': '合格', 'FAIL': '超限', 'UNCERTAIN': '不确定'}[restored_verdict]
    grades_before = {g['batch']: g['code'] for g in body['reconcile']['grades']}
    grades_after = {g['batch']: g['code'] for g in snapshot['reconcile']['grades']}
    # 样本判断翻转：被撤回项目的当前依据由复验ID换回原始报告ID，按(样本,项目)配对比对。
    before_by_key = {(c['sample_id'], c['analyte']): c for c in body['calculations']}
    verdict_changes = []
    for new_calc in snapshot['calculations']:
        old = before_by_key.get((new_calc['sample_id'], new_calc['analyte']))
        if old and old['code'] != new_calc['code']:
            verdict_changes.append({
                'sample_id': new_calc['sample_id'], 'analyte': new_calc['analyte'],
                'report_id': new_calc['id'], 'replaced_report_id': old['id'],
                'before': old['code'], 'after': new_calc['code'],
            })

    conclusion = {
        'withdrawal_id': withdrawal_key(run_id, report_id),
        'run_id': run_id,
        'report_id': report_id,
        'restored_report_id': target['replaces'],
        'reason': reason,
        'reason_text': ALLOWED_REASONS[reason],
        'withdrawn_at': withdrawn_at,
        'basis': '撤回后以原始报告 {orig} 作为当前依据，按采样日有效限量 {std}（{limit} mg/kg）判定'.format(
            orig=target['replaces'], std=target['standard'], limit=target['limit']),
        'sample_before': target['code'],
        'sample_after': restored_verdict,
        'verdict_changes': verdict_changes,
        'business_before': {'pass': before['pass'], 'fail': before['fail'], 'uncertain': before['uncertain'], 'total': before['total']},
        'business_after': {'pass': after['pass'], 'fail': after['fail'], 'uncertain': after['uncertain'], 'total': after['total']},
        'grades_before': grades_before,
        'grades_after': grades_after,
        'sealing': '封存口径跟随原始报告 {orig}：该样本/项目按原始结论（{word}）管控，批次按 FAIL>UNCERTAIN>PASS 重新归级'.format(
            orig=target['replaces'], word=sealing_word),
        # 原始与撤回报告均在采信测量留痕中可查，撤回只移出当前依据、不删除记录。
        'records_retained': sorted({m['id'] for m in body['measurements']
                                    if m['id'] in (report_id, target['replaces'])}),
    }
    assert set(conclusion['records_retained']) == {report_id, target['replaces']}

    new_body = deepcopy(body)
    new_body['measurements'] = snapshot['measurements']
    new_body['calculations'] = snapshot['calculations']
    new_body['business'] = snapshot['business']
    new_body['reconcile'] = snapshot['reconcile']
    new_body.setdefault('withdrawals', []).append(conclusion)
    return conclusion, new_body


def replay(body):
    """对已存运行体重放全部撤回结论，供工作台预览与归档导出使用。"""
    state = deepcopy(body)
    for w in state.get('withdrawals', []):
        state['measurements'] = [m for m in state['measurements'] if m['id'] != w['report_id']]
        state = _recalc(state)
    # 重放后仍以存证结论为准（_recalc 不触碰 withdrawals）。
    state['withdrawals'] = deepcopy(body.get('withdrawals', []))
    return state
