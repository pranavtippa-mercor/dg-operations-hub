#!/usr/bin/env python3
"""Refresh this dashboard and publish only an encrypted snapshot. No source mutations."""
import argparse,fcntl,hashlib,json,os,secrets,signal,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SERVICE='dg-operations-hub';ACCOUNT='pranavtippa-mercor';URL='https://pranavtippa-mercor.github.io/dg-operations-hub/'
def run(args,**kw):
    hosted=os.environ.get('GITHUB_ACTIONS','').lower()=='true'
    if hosted and 'capture_output' not in kw:
        kw.setdefault('stdout',subprocess.PIPE)
        kw.setdefault('stderr',subprocess.PIPE)
    try:
        return subprocess.run(args,cwd=ROOT,check=True,**kw)
    except subprocess.CalledProcessError as error:
        if not hosted:raise
        # Arguments/output can contain a private URL or provider evidence.
        # Keep the failure type/exit status needed by callers, without either.
        raise subprocess.CalledProcessError(error.returncode,'dashboard subprocess') from None
def access_key(hosted=False):
    configured=os.environ.get('DG_HUB_ACCESS_KEY')
    if configured is not None:
        if len(configured)<32:raise ValueError('The dashboard access key must have at least 32 characters.')
        return configured
    if hosted or os.environ.get('GITHUB_ACTIONS','').lower()=='true':
        raise RuntimeError('Hosted publishing requires DG_HUB_ACCESS_KEY; no local key is created.')
    existing=subprocess.run(['security','find-generic-password','-s',SERVICE,'-a',ACCOUNT,'-w'],capture_output=True,text=True)
    if existing.returncode==0:return existing.stdout.strip()
    value=secrets.token_urlsafe(32)
    # Keychain only; never written to the checkout, Git, logs, or Brain notes.
    stored=subprocess.run(['security','add-generic-password','-s',SERVICE,'-a',ACCOUNT,'-w',value],capture_output=True)
    if stored.returncode:raise RuntimeError('Unable to save the dashboard access key in macOS Keychain.')
    return value

REPO=ACCOUNT+'/dg-operations-hub'
BRANCH='dashboard-data'
MARKER='[dg-operations generated snapshot]'
def api(path,method='GET',body=None,missing=False):
    args=['gh','api','repos/'+REPO+path,'--method',method]
    if body is not None:args+=['--input','-']
    result=subprocess.run(args,input=json.dumps(body) if body is not None else None,capture_output=True,text=True,cwd=ROOT)
    if result.returncode:
        if missing and 'HTTP 404' in result.stderr:return None
        raise RuntimeError('GitHub data publication failed; prior data retained.')
    return json.loads(result.stdout) if result.stdout.strip() else None

class SnapshotCollectionError(RuntimeError):
    pass

def workflow_output(name, value):
    """Expose only fixed publication flags to the hosted workflow."""
    if name not in ('snapshot_ready', 'snapshot_published') or type(value) is not bool:
        raise ValueError('Invalid workflow publication flag.')
    destination=os.environ.get('GITHUB_OUTPUT')
    if destination and os.environ.get('GITHUB_ACTIONS','').lower()=='true':
        with open(destination,'a') as handle:
            handle.write(name+'='+str(value).lower()+'\n')

def publish(key,live,commit,last_hash,config_path=None):
    try:
        run([sys.executable,'scripts/collect.py']+(['--live'] if live else [])+(['--config',str(config_path)] if config_path else []))
    except subprocess.CalledProcessError:
        raise SnapshotCollectionError('Task snapshot collection failed.') from None
    raw=(ROOT/'.private/snapshot.json').read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest==last_hash:return digest
    env={**os.environ,'DG_HUB_ACCESS_KEY':key}
    run(['node','scripts/encrypt.mjs'],env=env)
    workflow_output('snapshot_ready',True)
    if commit:
        # The generated data branch is separate from source: no rebuild per refresh.
        existing=api('/git/ref/heads/'+BRANCH,missing=True)
        if existing:
            old=api('/git/commits/'+existing['object']['sha'])
            tree=api('/git/trees/'+old['tree']['sha'])
            if not old['message'].startswith(MARKER) or [e['path'] for e in tree['tree']]!=['snapshot.enc.json']:
                raise RuntimeError('Data branch has unrelated content; refusing to overwrite it.')
        encrypted=(ROOT/'.private/snapshot.enc.json').read_text()
        tree=api('/git/trees','POST',{'tree':[{'path':'snapshot.enc.json','mode':'100644','type':'blob','content':encrypted}]})
        new=api('/git/commits','POST',{'message':MARKER+' Refresh encrypted dashboard data','tree':tree['sha'],'parents':[]})
        if existing:api('/git/refs/heads/'+BRANCH,'PATCH',{'sha':new['sha'],'force':True})
        else:api('/git/refs','POST',{'ref':'refs/heads/'+BRANCH,'sha':new['sha']})
        workflow_output('snapshot_published',True)
    print('Encrypted snapshot ready.' if not commit else 'Encrypted snapshot published.',flush=True)
    return digest

def acquire_publisher_lock():
    path=ROOT/'.private/publisher.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+')
    try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('An operations publisher is already running.')
    handle.seek(0);handle.truncate();handle.write(str(os.getpid()));handle.flush()
    (ROOT/'.private/publisher.pid').write_text(str(os.getpid()))
    return handle

def run_once(key, config, config_path, commit, *, force=False):
    """Run due sources, then publish all usable evidence, reporting partial failure."""
    from collector_schedule import CollectorScheduler
    scheduler=CollectorScheduler(config)
    try:
        receipt=scheduler.run_due(force=force)
        # A retained source warning during its retry cooldown is not a new failed
        # attempt. The hosted health gate separately checks actual source age.
        failed=bool(receipt.get('attempt_failed',not receipt['ok']))
        try:
            publish(key,True,commit,None,config_path=config_path)
        except SnapshotCollectionError:
            failed=True
            print('Task refresh failed; assembling successful source updates with retained task evidence.',file=sys.stderr,flush=True)
            publish(key,False,commit,None,config_path=config_path)
        return 1 if failed else 0
    except (subprocess.CalledProcessError,ValueError,OSError,RuntimeError) as error:
        print('Refresh failed; the last published data is retained. '+type(error).__name__,file=sys.stderr,flush=True)
        return 1
    finally:
        scheduler.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');p.add_argument('--cached',action='store_true');p.add_argument('--push',action='store_true');p.add_argument('--open',action='store_true');p.add_argument('--copy-key',action='store_true');p.add_argument('--refresh-all',action='store_true');p.add_argument('--due-only',action='store_true');p.add_argument('--hosted',action='store_true');p.add_argument('--config',default=str(ROOT/'.private/config.json'));a=p.parse_args(argv)
    if a.due_only and (a.watch or a.cached or a.open or a.copy_key):
        p.error('--due-only is a finite live run and cannot be combined with --watch, --cached, --open, or --copy-key')
    key=access_key(hosted=a.hosted)
    # Hold the descriptor for this process; double-clicking the launcher cannot create duplicate publishers.
    publisher_lock=None if a.open or a.copy_key else acquire_publisher_lock()
    if a.copy_key:
        run(['pbcopy'],input=key.encode());print('Access key copied to clipboard.');return
    if a.open:
        run(['open',URL+'#key='+key],stdout=subprocess.DEVNULL);print('Opened the encrypted dashboard.');return
    from runtime_config import load_config
    config=load_config(a.config,root=ROOT)
    if a.due_only:
        try:
            return run_once(key,config,a.config,a.push,force=a.refresh_all)
        finally:
            publisher_lock.close()
    scheduler=None
    if not a.cached and config.get('version')==2:
        from collector_schedule import CollectorScheduler
        scheduler=CollectorScheduler(config)
        if a.refresh_all or not a.watch:
            try:scheduler.refresh_all()
            except RuntimeError:
                scheduler.close()
                return 1
    digest=None
    next_task=0
    stopping=False
    def request_stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,request_stop)
    try:
        while not stopping:
            sources_changed=scheduler.tick() if scheduler else False
            task_due=time.monotonic()>=next_task
            if task_due or sources_changed:
                started=time.monotonic()
                try:digest=publish(key,not a.cached and task_due,a.push,digest,config_path=a.config)
                except (subprocess.CalledProcessError,ValueError,OSError,RuntimeError) as e:
                    print('Refresh failed; the last published data is retained. '+type(e).__name__,file=sys.stderr,flush=True)
                    if not a.watch:return 1
                if not a.watch:return 0
                if task_due:next_task=started+config.get('schedules',{}).get('tasks_seconds',300)
            time.sleep(min(10,max(1,next_task-time.monotonic())))
    except KeyboardInterrupt:
        return 0
    finally:
        if scheduler:scheduler.close()
if __name__=='__main__':sys.exit(main())
