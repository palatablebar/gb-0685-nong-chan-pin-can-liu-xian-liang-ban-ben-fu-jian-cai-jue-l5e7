from flask import Blueprint,jsonify,render_template,request
from app.config import TITLE,SHORT,MODULES
from app.errors import DomainError
from app import store

bp=Blueprint('m8_withdrawal',__name__)


@bp.get('/m8_withdrawal')
def screen():
    latest=store.history()
    if latest:
        state=store.get(latest[0]['id'])
        from app.modules.m8_withdrawal.rules import replay
        data=replay(state)
    else:
        # 空库时仍给出可渲染的空壳，避免模板取空状态报错。
        from app.workflow import load, run
        state=run(load('dirty.csv'),{})
        data=state
    return render_template('m8_withdrawal.html',title=TITLE,short=SHORT,modules=MODULES,
                           current=8,data=data,run=state,history=latest,
                           withdrawals=state.get('withdrawals',[]))


@bp.route('/api/m8_withdrawal', methods=['GET', 'POST'])
def status():
    """模块接口约定：返回最新批次的撤回重放状态；无批次时返回空壳。"""
    latest = store.history()
    if not latest:
        return jsonify({'withdrawals': [], 'business': None, 'reconcile': None})
    from app.modules.m8_withdrawal.rules import replay
    state = replay(store.get(latest[0]['id']))
    return jsonify({
        'run_id': state['run_id'],
        'withdrawals': state.get('withdrawals', []),
        'business': state['business'],
        'reconcile': state['reconcile'],
        'calculations': state['calculations'],
    })


@bp.get('/api/runs/<run_id>/withdrawals')
def list_withdrawals(run_id):
    store.get(run_id)
    return jsonify(store.withdrawals(run_id))


@bp.post('/api/runs/<run_id>/withdrawals')
def withdraw(run_id):
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    conclusion,state=store.apply_withdrawal(run_id,data.get('report_id'),data.get('reason'))
    return jsonify({'conclusion':conclusion,'state':state})
