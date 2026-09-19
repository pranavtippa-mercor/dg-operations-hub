import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from runtime_config import load_config, normalize_config


class RuntimeConfigTests(unittest.TestCase):
    def test_paths_follow_checkout_without_mutating_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            config={'task_state': '.private/task-state.json', 'source_lock': '.private/source.lock',
                    'modules': {'cache_dir': '.private/modules', 'data': '.private/modules/data.json'},
                    'slack': {'cache_dir': str(root/'absolute-slack')},
                    'runtime_mode': 'Hosted collectors (GitHub Actions)'}
            result=normalize_config(config,root=root)
            self.assertEqual(result['task_state'],str(root/'.private/task-state.json'))
            self.assertEqual(result['source_lock'],str(root/'.private/source.lock'))
            self.assertEqual(result['modules']['data'],str(root/'.private/modules/data.json'))
            self.assertEqual(result['slack']['cache_dir'],str(root/'absolute-slack'))
            self.assertEqual(config['task_state'],'.private/task-state.json')
            self.assertEqual(result['runtime_mode'],'Hosted collectors (GitHub Actions)')
            file=root/'config.json';file.write_text(json.dumps(config))
            self.assertEqual(load_config(file,root=root),result)
            self.assertEqual(load_config('config.json',root=root),result)

    def test_runtime_mode_and_paths_reject_malformed_values(self):
        for config in ({'runtime_mode': []},{'runtime_mode':'bad\nlabel'},
                       {'runtime_mode':'x'*81},{'task_state':''},{'modules': []},
                       {'slack':{'cache_dir':[]}}):
            with self.assertRaises(ValueError):
                normalize_config(config)


if __name__=='__main__':
    unittest.main()
