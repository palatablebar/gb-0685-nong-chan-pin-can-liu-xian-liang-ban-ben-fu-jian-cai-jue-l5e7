import sqlite3,json,os
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 撤回处理结论独立留痕：同一(run,report)只允许一条，重跑不新增记录；原始与撤回报告均不删除。
    db.execute('''CREATE TABLE IF NOT EXISTS withdrawals
        (withdrawal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, report_id TEXT NOT NULL,
         body TEXT NOT NULL, created_at TEXT NOT NULL,
         UNIQUE(run_id,report_id))''')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def withdrawals(run_id):
    with connection() as db:
        rows=db.execute('SELECT body FROM withdrawals WHERE run_id=? ORDER BY rowid',(run_id,)).fetchall()
    return [json.loads(r['body']) for r in rows]

def apply_withdrawal(run_id, report_id, reason):
    """事务化撤回：任一步失败整体回滚，恢复旧版结论；同一撤回重跑幂等不新增记录。"""
    from app.modules.m8_withdrawal.rules import build_withdrawal, withdrawal_key
    db=connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT body FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:raise DomainError('NOT_FOUND','批次不存在',404)
        body=json.loads(row['body'])
        wid=withdrawal_key(run_id,report_id.strip() if isinstance(report_id,str) else report_id)
        existing=db.execute('SELECT body FROM withdrawals WHERE withdrawal_id=?',(wid,)).fetchone()
        if existing:
            # 幂等：同一撤回重跑返回既有结论，不新增记录、不改动当前依据。
            return json.loads(existing['body']),get(run_id)
        withdrawn_at=datetime.now(timezone.utc).isoformat()
        conclusion,new_body=build_withdrawal(body,report_id,reason,withdrawn_at)
        cur=db.execute('UPDATE runs SET body=? WHERE id=? AND body=?',
                       (json.dumps(new_body,ensure_ascii=False),run_id,row['body']))
        if cur.rowcount!=1:
            # 并发改写（如复核签字已落库）：放弃本次处理，回滚恢复旧版结论。
            raise DomainError('STALE','批次结论已被更新，请刷新后重试',409)
        db.execute('INSERT INTO withdrawals(withdrawal_id,run_id,report_id,body,created_at) VALUES (?,?,?,?,?)',
                   (conclusion['withdrawal_id'],run_id,conclusion['report_id'],
                    json.dumps(conclusion,ensure_ascii=False),withdrawn_at))
        db.commit()
    except Exception:
        db.rollback();raise
    finally:
        db.close()
    return conclusion,get(run_id)
