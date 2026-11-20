import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def api(stage):
    content,parameters=payload();return jsonify(run(content,parameters,stage))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage)
    # 已落库的撤回结论需要在预览页重放，保证页面口径与封存证据一致。
    if state.get('withdrawals'):
        from app.modules.m8_withdrawal.rules import replay
        replayed=replay(state)
        data['calculations']=replayed['calculations'];data['business']=replayed['business']
        data['reconcile']=replayed['reconcile'];data['withdrawals']=replayed['withdrawals']
        data['measurements']=replayed['measurements']
        if stage>=7:
            from app.modules.m7_archive.export_out import checksum
            data['archive']={'sha256':checksum(data),'evidence_count':len(data['calculations'])+len(data['quarantine'])+len(data['withdrawals']),'ready':data['reconcile'].get('balanced',False)}
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest)
