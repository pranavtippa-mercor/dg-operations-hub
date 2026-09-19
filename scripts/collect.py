#!/usr/bin/env python3
"""Build one private, validated snapshot. No source files or Studio records are changed."""
import argparse, json, os, re, sys, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PT = ZoneInfo('America/Los_Angeles')
sys.path.insert(0, str(ROOT / 'scripts'))
from source_client import SourceClient
from runtime_config import load_config, normalize_config

def load(path): return json.loads(Path(path).read_text())
def iso(dt): return dt.astimezone(UTC).isoformat().replace('+00:00', 'Z')
def parse(value): return datetime.fromisoformat(value.replace('Z','+00:00')) if value else None

def deadline(raw, sidecar):
    if raw:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            d = datetime.fromisoformat(raw).replace(hour=23,minute=59,tzinfo=PT)
        else:
            d = parse(raw)
            if d.tzinfo is None: raise ValueError('Timestamp deadline needs a timezone')
        return {'at':iso(d), 'source':'Studio rfd_due', 'url':None, 'raw':raw}
    # A current explicit commitment is useful only when Studio has no deadline.
    assigned = sidecar.get('assigned') or {}
    chosen = sidecar.get('committed') or (assigned if assigned.get('source') != 'studio' else None)
    if chosen and chosen.get('due'):
        return {'at':iso(parse(chosen['due'])), 'source':'Commitment fallback' if sidecar.get('committed') else 'Assigned fallback', 'url':chosen.get('url'), 'raw':chosen['due']}
    return None

def validate_modules(doc):
    width=doc['n_hours']
    if not isinstance(width,int) or width<1 or width>1000: raise ValueError('Invalid module hour count')
    for module in doc['modules']:
        for row,den,total in [(module,'d',2)]+[(d,'g',3) for d in module['dims']]:
            keys=['f',den]+(['n'] if total==3 else [])
            for key in keys:
                if len(row[key])!=width or any(type(v)!=int or v<0 for v in row[key]): raise ValueError('Invalid module array')
            if any(f>d for f,d in zip(row['f'],row[den])): raise ValueError('Failure numerator exceeds denominator')
            if row['t']!=[sum(row[k]) for k in keys]: raise ValueError('Module totals do not reconcile')

def collect(config, live=False):
    config = normalize_config(config, root=ROOT)
    if config.get('version') != 2:
        raise ValueError('Migrate sources to the independent version 2 configuration first.')
    state = load(config['task_state'])
    seed, rows, status, users = (state[k] for k in ('seed','rows','statuses','users'))
    slack_dir = Path(config['slack']['cache_dir'])
    annotations, history = state['annotations'], state['history']
    fixed_html, root_map = state['fixed_html'], state['roots']
    if (slack_dir/'bundle.json').exists():
        evidence = load(slack_dir/'bundle.json')
        dues, slack, confirmations_doc, slack_status, root_map = (evidence[k] for k in ('commitments','activity','confirmations','state','roots'))
    else:
        # Imported seed files are needed only before the first collector run.
        dues = load(slack_dir/'commitments.json')
        slack = load(slack_dir/'activity.json')
        confirmations_doc = load(slack_dir/'confirmations.json')
        slack_status = load(slack_dir/'state.json') if (slack_dir/'state.json').exists() else {}
    activity_at=state.get('studio_read_at')
    task_at=seed.get('reverified_at') or seed['observed_at']
    sdefs={s['id']:s for s in seed['statuses']}
    for sid,name in status.items(): sdefs.setdefault(sid, {'id':sid,'name':name,'rfd':False})
    owners={uid:re.sub(r'^\[\w+\]\s*','',u['name']) for uid,u in users.items()}
    owners.update(seed['owners'])
    confirmations=confirmations_doc.get('tasks', {})
    confirmation_at=confirmations_doc.get('harvested_at')
    def normalize_confirmation(name):
        rec=confirmations.get(name) or confirmations.get(re.sub(r'-TEMPLATE$', '', name))
        if not rec:return None
        def stamp(value):
            return iso(datetime.fromtimestamp(float(value),UTC)) if value else None
        return {'state':rec.get('state') or 'not_checked','asked':stamp(rec.get('asked_at')), 'reply':stamp(rec.get('reply_at')), **{k:rec.get(k) for k in ('asked_by','asked_url','deadline','writer','reply_by','reply_text','reply_url','observed_at','checked_at','thread_gap')}}
    bridge_module=None
    if live:
        bridge_module = SourceClient(config, upstream_tools=['studio'])
        # Same authorized connection, one superset SELECT for both task views.
        query=("SELECT t.task_id,t.task_name,t.task_status_id AS status,t.owned_by,t.updated_by,t.updated_at,t.transitioned_at,t.version,"
               "t.custom_fields->>'artifact_type' AS artifact,t.custom_fields->>'task_mode' AS task_mode,"
               "t.custom_fields->>'rfd_due' AS rfd_due,t.custom_fields->>'domain' AS domain,"
               "t.custom_fields->>'blocked' AS blocked,t.custom_fields->>'reviewer' AS reviewer,"
               "t.custom_fields->>'expert' AS expert,t.custom_fields->>'writer' AS writer FROM tasks t "
               "WHERE t.world_id='"+seed['world']+"' AND t.archived_at IS NULL ORDER BY t.task_name LIMIT 1000")
        try:
            response=bridge_module.studio('POST','/querier/unstructured',{'query':query})
            task_read_at=iso(datetime.now(UTC))
            rows=response.get('rows',[])
            if not rows or len(rows)>=1000: raise ValueError('Empty or capped Studio result; prior snapshot retained')
            world=bridge_module.studio('GET','/worlds/'+seed['world'])
            for s in world['status_config']['status_defns']:
                sdefs[s['status_id']]={'id':s['status_id'],'name':s['status_name'],'rfd':s.get('qualifies_ready_for_delivery',False)}
            if {r.get('owned_by') for r in rows if r.get('owned_by')}-owners.keys():
                for u in bridge_module.studio('GET','/users/campaign/'+seed['campaign']):
                    owners[u['user_id']]=' '.join(filter(None,[u.get('first_name'),u.get('last_name')])) or 'Unknown owner'
            activity_at=task_at=task_read_at
        finally:
            bridge_module.close()
    by_id={r['task_id']:r for r in rows}
    if len(by_id)!=len(rows): raise ValueError('Duplicate Studio task IDs')
    completion={t['id']:(k,t) for k,tr in seed['tracks'].items() for t in tr['tasks']}
    # Existing verified roster must survive both imports and live refreshes.
    previous_path=ROOT/'.private/snapshot.json'
    previous=load(previous_path) if previous_path.exists() else {}
    expected_ids=set(completion)|{t['id'] for t in previous.get('tasks',[])}
    missing=expected_ids-set(by_id)
    if missing: raise ValueError(f'Incomplete activity roster: {len(missing)} task IDs missing')
    observed_names={r.get('task_name') for r in rows}
    if set(fixed_html)-observed_names:raise ValueError('Verified HTML roster is incomplete')
    tasks=[]; conflicts=[]
    def domain_of(name):
        m=re.match(r'DOP-TC26-BASE-([A-Z]{3})-',name) or re.match(r'DOP-XLSX-([A-Z]{3})-',name) or re.match(r'DOP2?-([A-Z]{3})-',name)
        return m.group(1) if m else '?'
    for r in rows:
        name=r['task_name']; tid=r['task_id']
        if any(s in name for s in ['TEST_ONLY','SYNTHETIC_CANARY','BASELINE_G2']) or re.match(r'^(TEST[-_]|RUBRIC-)',name): continue
        old=completion.get(tid)
        cohort=('tpl' if name.startswith('DOP-TC26-BASE-') and r.get('task_mode')=='Base Task' else r.get('artifact') if r.get('task_mode')=='Edit Eval' else None)
        if cohort not in ('html','docx','pptx','xlsx','tpl'): continue
        if cohort=='html' and name not in fixed_html:continue
        t=old[1] if old else {}
        sid=r['status']
        if sid not in sdefs: raise ValueError('Unknown Studio stage; refresh status definitions')
        stage=sdefs[sid]['name']
        if stage=='Discarded': continue
        moved=r.get('transitioned_at')
        owner_id=r.get('owned_by')
        fallback=r.get('reviewer') or r.get('expert') or r.get('writer')
        owner=owners.get(owner_id,'Unknown owner') if owner_id else 'Unclaimed'
        side=dues.get(name,{})
        root=root_map.get(name) or root_map.get(re.sub(r'-TEMPLATE$','',name))
        slack_url=t.get('slack') or (f"https://mercor.enterprise.slack.com/archives/{root['channel']}/p{root['ts'].replace('.','')}" if root else None)
        tasks.append({'id':tid,'name':name,'cohort':cohort,'artifact':r.get('artifact'),'domain':t.get('domain') or domain_of(name),
          'stage':stage,'status_id':sid,'rfd':bool(sdefs[sid].get('rfd')),'owner':owner,'owner_id':owner_id,'historical_holder':fallback,'last_actor':owners.get(r.get('updated_by'),'Unknown'),
          'updated_at':r.get('updated_at'),'transitioned_at':moved,'activity_conflict':not activity_at,'blocked':str(r.get('blocked')).lower()=='true',
          'deadline':deadline(r.get('rfd_due'),side),'deadline_history':side,'slack_activity':slack.get(name),'confirmation':normalize_confirmation(name),
          'warning':annotations.get(tid) or annotations.get(name),
          'studio':t.get('studio') or f"https://studio.mercor.com/annotator/tasks/{tid}/?campaignId={seed['campaign']}",'slack':slack_url})
    modules=load(config['modules']['data']);validate_modules(modules)
    local_start=datetime.strptime(modules['window_start_local'], '%Y-%m-%d %H').replace(tzinfo=PT)
    if 'hours' not in modules:
        modules['hours']=[iso(local_start.astimezone(UTC)+timedelta(hours=i)) for i in range(modules['n_hours'])]
    if modules.get('world_id')!=seed['world']: raise ValueError('Task/module production worlds differ')
    if not tasks or len({t['id'] for t in tasks})!=len(tasks): raise ValueError('Invalid task roster')
    now=iso(datetime.now(UTC))
    coverage = slack_status.get('coverage', {})
    slack_scope = f" · {coverage['threads_complete']}/{coverage['tasks']} task threads" if 'threads_complete' in coverage else ''
    if live:
        # The successful Studio read becomes the fallback, independently of legacy files.
        state.update(rows=rows, statuses={sid:d['name'] for sid,d in sdefs.items()}, users={uid:{'name':name} for uid,name in owners.items()}, studio_read_at=task_at)
        state['seed'].update(observed_at=task_at, reverified_at=task_at, statuses=list(sdefs.values()), owners=owners)
        atomic_write(config['task_state'], state)
    return {'schema_version':1,'generated_at':now,'tasks':tasks,'modules':modules,
      'sources':{'tasks':{'at':task_at,'cadence_minutes':5,'label':'Studio task inventory'},'activity':{'at':activity_at,'cadence_minutes':5,'label':'Studio activity'},
      'confirmations':{'at':slack_status.get('collected_at') or confirmation_at,'cadence_minutes':15,'label':'Slack confirmations checked'+slack_scope},
      'slack':{'at':slack_status.get('collected_at') or max((s.get('seen_at') or s.get('at') for s in slack.values()),default=None),'cadence_minutes':15,'label':'Slack activity'+slack_scope},
      'modules':{'at':modules.get('snapshot_at_utc') or modules['generated_at_utc'],'cadence_minutes':60,'label':'Module failure aggregates'}},
      'history':history,'conflicts':conflicts,'mode':config['runtime_mode']}

def atomic_write(path,doc):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(doc,separators=(',',':')));os.chmod(tmp,0o600);tmp.replace(path)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',default=str(ROOT/'.private/config.json'));parser.add_argument('--live',action='store_true');parser.add_argument('--out',default=str(ROOT/'.private/snapshot.json'))
    args=parser.parse_args();doc=collect(load_config(args.config),args.live)
    pending=Path(args.out).with_name('pending-snapshot.json')
    atomic_write(pending,doc)
    try:
        subprocess.run(['node','--experimental-strip-types','scripts/finalize.mts',str(pending)]+(['--history'] if args.live else []),cwd=ROOT,check=True)
        pending.replace(args.out)
    finally:
        pending.unlink(missing_ok=True)
    print(json.dumps({'ok':True,'tasks':len(doc['tasks']),'cohorts':{k:sum(t['cohort']==k for t in doc['tasks']) for k in ('html','docx','pptx','xlsx','tpl')},'modules':len(doc['modules']['modules']),'source_conflicts':len(doc['conflicts']),'generated_at':doc['generated_at']}))
if __name__=='__main__': main()
