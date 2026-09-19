"""Fail the publish if identifiable task data, raw snapshots, or credentials leak into static output."""
from pathlib import Path
import re,sys
root=Path(sys.argv[1]) if len(sys.argv)>1 else Path('out')
errors=[]
for p in root.rglob('*'):
 if not p.is_file():continue
 if p.name=='snapshot.enc.json':
  import json
  obj=json.loads(p.read_text())
  if set(obj)!={'format','compression','salt','iv','iterations','ciphertext'} or obj['format']!='dg-operations-encrypted-v1':errors.append(str(p))
  continue
 if p.suffix not in ('.html','.js','.json','.txt','.map','.css'):continue
 s=p.read_text(errors='replace')
 if re.search(r'task_[a-f0-9]{20,}|world_[a-f0-9]{20,}|DOP(?:2)?-(?:[A-Z]{3}-ST|XLSX-|TC26-BASE-|[A-Z]{3}-APP)|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{25,}',s):errors.append(str(p))
if errors:raise SystemExit('Private data found in public output: '+', '.join(errors))
print('Public build privacy check passed.')
