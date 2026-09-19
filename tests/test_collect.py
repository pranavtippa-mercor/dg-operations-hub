import importlib.util,unittest
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
if __name__=='__main__':unittest.main()
