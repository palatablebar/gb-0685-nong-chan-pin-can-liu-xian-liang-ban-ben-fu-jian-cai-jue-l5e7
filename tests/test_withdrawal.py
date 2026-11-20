import csv, io, os, sqlite3
import pytest
from app import store
from app.errors import DomainError
from app.workflow import load, run
from app.modules.m7_archive.export_out import bundle
from app.modules.m8_withdrawal.rules import build_withdrawal, replay, withdrawal_key


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'w.db'))
    body = run(load('dirty.csv'), {})
    store.save(body['run_id'], body)
    return body


def test_withdrawal_restores_original_as_current_basis(saved_run):
    """裁决核心：撤回后以原始报告作为当前依据，样本判定跟随原始结论。"""
    con, new_body = build_withdrawal(saved_run, '00002', 'LIMIT_VERSION', '2026-11-20T00:00:00+00:00')
    assert con['restored_report_id'] == '00001'
    assert con['sample_before'] == 'PASS' and con['sample_after'] == 'UNCERTAIN'
    calc = {c['id']: c for c in new_body['calculations']}
    assert '00001' in calc and '00002' not in calc
    assert calc['00001']['code'] == 'UNCERTAIN'
    assert con['verdict_changes'] == [{
        'sample_id': 'S001', 'analyte': 'P01', 'report_id': '00001',
        'replaced_report_id': '00002', 'before': 'PASS', 'after': 'UNCERTAIN'}]


def test_business_accounts_and_batch_grades_follow_conclusion(saved_run):
    """样本判断、业务账与批次归级统一跟随撤回结论。"""
    _, new_body = build_withdrawal(saved_run, '00002', 'LIMIT_VERSION', 't')
    assert new_body['business'] == {'total': 24, 'pass': 11, 'fail': 12, 'uncertain': 1, 'unit': '检测项目'}
    # P02 仍 FAIL，批次按 FAIL>UNCERTAIN>PASS 归级，四批维持 FAIL。
    assert {g['batch']: g['code'] for g in new_body['reconcile']['grades']} == {
        'B001': 'FAIL', 'B002': 'FAIL', 'B003': 'FAIL', 'B004': 'FAIL'}
    assert new_body['reconcile']['balanced'] is True


def test_multiple_withdrawals_accumulate_and_replay_matches(saved_run):
    _, b1 = build_withdrawal(saved_run, '00002', 'LIMIT_VERSION', 't1')
    con2, b2 = build_withdrawal(b1, '00005', 'LIMIT_VERSION', 't2')
    assert b2['business'] == {'total': 24, 'pass': 10, 'fail': 12, 'uncertain': 2, 'unit': '检测项目'}
    assert [w['report_id'] for w in b2['withdrawals']] == ['00002', '00005']
    replayed = replay(b2)
    assert replayed['business'] == b2['business']
    assert replayed['calculations'] == b2['calculations']
    assert replayed['reconcile']['grades'] == b2['reconcile']['grades']
    assert replayed['withdrawals'] == b2['withdrawals']


def test_original_and_withdrawn_reports_are_never_lost(saved_run):
    con, new_body = build_withdrawal(saved_run, '00002', 'LIMIT_VERSION', 't')
    # 留痕里两份报告都在，撤回只把复验移出当前依据。
    assert set(con['records_retained']) == {'00001', '00002'}
    ids = {m['id'] for m in saved_run['measurements']}
    assert {'00001', '00002'} <= ids
    # 归档证据包保留撤回结论与原始/撤回报告ID。
    text = bundle(new_body)
    assert '撤回' in text and '00001' in text and '00002' in text


def test_withdrawal_rejects_non_retest_and_bad_reason(saved_run):
    with pytest.raises(DomainError) as e:
        build_withdrawal(saved_run, '00001', 'LIMIT_VERSION', 't')  # 原始报告不可撤回
    assert e.value.code == 'REPORT'
    with pytest.raises(DomainError) as e:
        build_withdrawal(saved_run, '99999', 'LIMIT_VERSION', 't')  # 不存在
    assert e.value.code == 'REPORT'
    with pytest.raises(DomainError) as e:
        build_withdrawal(saved_run, '00002', 'BAD_REASON', 't')
    assert e.value.code == 'REASON'


def test_pure_function_does_not_mutate_input(saved_run):
    before = saved_run['business']
    build_withdrawal(saved_run, '00002', 'LIMIT_VERSION', 't')
    assert saved_run['business'] == before
    assert 'withdrawals' not in saved_run


def test_store_idempotent_rerun_creates_no_new_record(saved_run):
    rid = saved_run['run_id']
    con1, st1 = store.apply_withdrawal(rid, '00002', 'LIMIT_VERSION')
    con2, st2 = store.apply_withdrawal(rid, '00002', 'LIMIT_VERSION')
    assert con1['withdrawal_id'] == con2['withdrawal_id'] == withdrawal_key(rid, '00002')
    assert st1['business'] == st2['business']
    db = sqlite3.connect(os.environ['DB_PATH'])
    assert db.execute('SELECT COUNT(*) FROM withdrawals').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
    assert len(store.withdrawals(rid)) == 1


def test_store_rollback_restores_old_conclusion(saved_run, monkeypatch):
    """处理中断必须恢复旧版结论，且不留下半截撤回记录。"""
    rid = saved_run['run_id']
    store.apply_withdrawal(rid, '00002', 'LIMIT_VERSION')
    before = store.get(rid)['business']
    import app.modules.m8_withdrawal.rules as rules
    monkeypatch.setattr(rules, 'build_withdrawal', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        store.apply_withdrawal(rid, '00005', 'LIMIT_VERSION')
    assert store.get(rid)['business'] == before
    db = sqlite3.connect(os.environ['DB_PATH'])
    assert db.execute('SELECT COUNT(*) FROM withdrawals').fetchone()[0] == 1


def test_store_distinct_withdrawal_adds_one_record(saved_run):
    rid = saved_run['run_id']
    store.apply_withdrawal(rid, '00002', 'LIMIT_VERSION')
    store.apply_withdrawal(rid, '00005', 'LIMIT_VERSION')
    rows = store.withdrawals(rid)
    assert [r['report_id'] for r in rows] == ['00002', '00005']
    assert store.get(rid)['business']['uncertain'] == 2


def test_store_missing_run_404(saved_run):
    with pytest.raises(DomainError) as e:
        store.apply_withdrawal('absent', '00002', 'LIMIT_VERSION')
    assert e.value.status == 404


def test_store_stale_body_aborts_and_rolls_back(saved_run, monkeypatch):
    """条件更新未命中（并发改写）：返回409并整体回滚，旧版结论不动。"""
    rid = saved_run['run_id']

    class FakeCursor:
        def __init__(self): self.rowcount = 0
        def execute(self, *a): return self

    class FakeDB:
        def __init__(self, real): self._real = real
        def execute(self, sql, params=()):
            if sql.startswith('UPDATE runs'):
                return FakeCursor()
            return self._real.execute(sql, params)
        def commit(self): pass
        def rollback(self): self._real.rollback()
        def close(self): self._real.close()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    real_connection = store.connection
    monkeypatch.setattr(store, 'connection', lambda: FakeDB(real_connection()))
    with pytest.raises(DomainError) as e:
        store.apply_withdrawal(rid, '00002', 'LIMIT_VERSION')
    assert e.value.status == 409 and e.value.code == 'STALE'
    assert store.get(rid)['business'] == saved_run['business']
    assert store.withdrawals(rid) == []


def test_http_withdraw_and_idempotent_rerun(client):
    s = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    rid = s['run_id']
    r = client.post(f'/api/runs/{rid}/withdrawals', json={'report_id': '00002', 'reason': 'LIMIT_VERSION'})
    assert r.status_code == 200
    wid = r.get_json()['conclusion']['withdrawal_id']
    r2 = client.post(f'/api/runs/{rid}/withdrawals', json={'report_id': '00002', 'reason': 'LIMIT_VERSION'})
    assert r2.status_code == 200 and r2.get_json()['conclusion']['withdrawal_id'] == wid
    assert len(client.get(f'/api/runs/{rid}/withdrawals').get_json()) == 1
    assert client.post(f'/api/runs/{rid}/withdrawals', json={'report_id': '00001'}).status_code == 422
    assert client.post('/api/runs/absent/withdrawals', json={'report_id': '00002'}).status_code == 404


def test_http_export_and_pages_reflect_withdrawal(client):
    s = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    rid = s['run_id']
    client.post(f'/api/runs/{rid}/withdrawals', json={'report_id': '00002', 'reason': 'LIMIT_VERSION'})
    page = client.get('/m8_withdrawal')
    assert page.status_code == 200 and '撤回处理结论' in page.data.decode()
    export = client.get(f'/api/archive/{rid}/export.csv')
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
    kinds = {row[0] for row in rows[1:]}
    assert '撤回' in kinds
    stored = client.get(f'/api/runs/{rid}').get_json()
    assert stored['business']['uncertain'] == 1 and stored['reconcile']['balanced'] is True
    # 撤回复验不再出现在计算证据，原始报告恢复出现。
    calc_ids = {row[3] for row in rows if row[0] == '计算'}
    assert '00001' in calc_ids and '00002' not in calc_ids


def test_review_still_guarded_after_withdrawal(client):
    """既有复核条件更新机制不受撤回改造影响。"""
    s = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    rid = s['run_id']
    client.post(f'/api/runs/{rid}/withdrawals', json={'report_id': '00002'})
    ok = client.post(f'/api/runs/{rid}/review', json={'version': 1, 'decision': '通过'})
    assert ok.status_code == 200 and ok.get_json()['review_version'] == 2
    stale = client.post(f'/api/runs/{rid}/review', json={'version': 1, 'decision': '退回'})
    assert stale.status_code == 409


def test_default_reason_and_sealing_text(saved_run):
    con, _ = build_withdrawal(saved_run, '00002', None, 't')
    assert con['reason'] == 'LIMIT_VERSION'
    assert con['restored_report_id'] in con['sealing'] and '封存口径' in con['sealing']
    assert con['basis'].startswith('撤回后以原始报告 00001 作为当前依据')


def test_empty_history_page_renders(client, monkeypatch):
    from app import store
    monkeypatch.setattr(store, 'history', lambda: [])
    r = client.get('/m8_withdrawal')
    assert r.status_code == 200 and '暂无撤回记录' in r.data.decode()
    assert client.get('/api/m8_withdrawal').status_code == 200
