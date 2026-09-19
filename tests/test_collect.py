import importlib.util,unittest,json,tempfile
from unittest.mock import patch
from pathlib import Path
s=importlib.util.spec_from_file_location('collector',Path(__file__).resolve().parents[1]/'scripts/collect.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class CollectorTests(unittest.TestCase):
 def test_studio_wins(self):
  got=m.deadline('2026-09-17',{'committed':{'due':'2026-09-12T06:59:00Z'}})
  self.assertEqual(got['at'],'2026-09-18T06:59:00Z');self.assertEqual(got['source'],'Studio rfd_due')
 def test_winter_deadline(self):self.assertEqual(m.deadline('2026-11-01',{})['at'],'2026-11-02T07:59:00Z')
 def test_removed_studio_deadline_not_resurrected(self):self.assertIsNone(m.deadline(None,{'assigned':{'source':'studio','due':'2026-09-12T06:59:00Z'}}))
 def test_real_fallback_retained(self):self.assertEqual(m.deadline(None,{'assigned':{'source':'thread','due':'2026-09-12T06:59:00Z'}})['source'],'Assigned fallback')
 def test_naive_timestamp_rejected(self):
  with self.assertRaises(ValueError):m.deadline('2026-09-17T15:00:00',{})
 def test_module_bad_total_rejected(self):
  with self.assertRaises(ValueError):m.validate_modules({'n_hours':1,'modules':[{'f':[1],'d':[2],'t':[0,2],'dims':[]}]})
 def test_module_zero_is_valid(self):m.validate_modules({'n_hours':1,'modules':[{'f':[0],'d':[0],'t':[0,0],'dims':[{'f':[0],'g':[0],'n':[3],'t':[0,0,3]}]}]})
 def test_independent_cache_uses_latest_rows_not_historical_seed(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);slack=root/'slack';slack.mkdir()
   def save(path,value):path.write_text(json.dumps(value));return str(path)
   at='2026-09-18T12:00:00Z'
   state={'seed':{'observed_at':at,'world':'example-world','campaign':'example-campaign','statuses':[{'id':'old','name':'Writing','rfd':False},{'id':'new','name':'Ready for Delivery','rfd':True}],'owners':{'prior':'Prior Owner','current':'Current Owner'},'tracks':{'docx':{'tasks':[{'id':'example-one','status':'old','owner':'prior','moved':'2026-09-17T12:00:00Z'}]}}},'rows':[{'task_id':'example-one','task_name':'EXAMPLE-001','status':'new','owned_by':'current','artifact':'docx','task_mode':'Edit Eval','transitioned_at':at},{'task_id':'example-two','task_name':'EXAMPLE-002','status':'new','owned_by':None,'artifact':'docx','task_mode':'Edit Eval','transitioned_at':at}],'statuses':{},'users':{},'annotations':{},'history':[],'fixed_html':{},'roots':{},'studio_read_at':at}
   for name,value in [('commitments.json',{}),('activity.json',{}),('confirmations.json',{'tasks':{}})]:save(slack/name,value)
   config={'version':2,'runtime_mode':'Hosted collectors','task_state':save(root/'tasks.json',state),'slack':{'cache_dir':str(slack)},'modules':{'data':save(root/'modules.json',{'world_id':'example-world','generated_at_utc':at,'snapshot_at_utc':'2026-09-18T11:55:00Z','n_hours':1,'window_start_local':'2026-09-18 00','modules':[]})}}
   with patch.object(m,'ROOT',root):data=m.collect(config)
   self.assertEqual(len(data['tasks']),2)
   self.assertEqual(data['tasks'][0]['stage'],'Ready for Delivery')
   self.assertEqual(data['tasks'][0]['owner'],'Current Owner')
   self.assertEqual(data['tasks'][0]['transitioned_at'],at)
   self.assertFalse(data['tasks'][0]['activity_conflict'])
   self.assertEqual(data['sources']['modules']['at'],'2026-09-18T11:55:00Z')
   self.assertEqual(data['mode'],'Hosted collectors')
if __name__=='__main__':unittest.main()
