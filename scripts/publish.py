#!/usr/bin/env python3
"""Refresh this dashboard and publish only an encrypted snapshot. No source mutations."""
import argparse,hashlib,json,os,secrets,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SERVICE='dg-operations-hub';ACCOUNT='pranavtippa-mercor';URL='https://pranavtippa-mercor.github.io/dg-operations-hub/'
def run(args,**kw):return subprocess.run(args,cwd=ROOT,check=True,**kw)
def access_key():
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

def publish(key,live,commit,last_hash):
    run([sys.executable,'scripts/collect.py']+(['--live'] if live else []))
    raw=(ROOT/'.private/snapshot.json').read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest==last_hash:return digest
    env={**os.environ,'DG_HUB_ACCESS_KEY':key}
    run(['node','scripts/encrypt.mjs'],env=env)
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
    print('Encrypted snapshot ready.' if not commit else 'Encrypted snapshot published.',flush=True)
    return digest

def main():
    p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');p.add_argument('--cached',action='store_true');p.add_argument('--push',action='store_true');p.add_argument('--open',action='store_true');p.add_argument('--copy-key',action='store_true');a=p.parse_args()
    key=access_key()
    if a.copy_key:
        run(['pbcopy'],input=key.encode());print('Access key copied to clipboard.');return
    if a.open:
        run(['open',URL+'#key='+key],stdout=subprocess.DEVNULL);print('Opened the encrypted dashboard.');return
    digest=None
    while True:
        try:digest=publish(key,not a.cached,a.push,digest)
        except (subprocess.CalledProcessError,ValueError,OSError,RuntimeError) as e:
            print('Refresh failed; the last published data is retained. '+type(e).__name__,file=sys.stderr,flush=True)
            if not a.watch:return 1
        if not a.watch:return 0
        time.sleep(300)
if __name__=='__main__':sys.exit(main())
