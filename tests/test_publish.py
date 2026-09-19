import importlib.util, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('publisher',Path(__file__).resolve().parents[1]/'scripts/publish.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
class PublisherTests(unittest.TestCase):
 def test_only_one_publisher_then_release(self):
  with tempfile.TemporaryDirectory() as directory:
   original=publisher.ROOT;publisher.ROOT=Path(directory)
   try:
    first=publisher.acquire_publisher_lock()
    try:
     with self.assertRaisesRegex(RuntimeError,'already running'):publisher.acquire_publisher_lock()
    finally:first.close()
    second=publisher.acquire_publisher_lock();second.close()
   finally:publisher.ROOT=original
if __name__=='__main__':unittest.main()
