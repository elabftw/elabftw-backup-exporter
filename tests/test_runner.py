"""Check launcher exit/cleanup behavior without requiring a Docker daemon."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FAKE_DOCKER = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['DOCKER_TEST_LOG'], 'a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='exec' and '-i' in args:
    Path(os.environ['DOCKER_TEST_SQL']).write_bytes(sys.stdin.buffer.read())
    sys.exit(int(os.environ.get('DOCKER_TEST_IMPORT_CODE','0')))
if args[0]=='inspect':print('true')
if args[0]=='run' and '-d' not in args:sys.exit(int(os.environ.get('DOCKER_TEST_EXPORT_CODE','0')))
'''


class RunnerTests(unittest.TestCase):
    def run_launcher(self, import_code=0, export_code=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        binary = root / 'bin'
        binary.mkdir()
        (binary / 'docker').write_text(FAKE_DOCKER)
        (binary / 'docker').chmod(0o755)
        log, restored = root / 'docker-log.jsonl', root / 'restored.sql'
        env = os.environ.copy()
        env.update(PATH=str(binary)+os.pathsep+env['PATH'], DOCKER_TEST_LOG=str(log),
                   DOCKER_TEST_SQL=str(restored), DOCKER_TEST_IMPORT_CODE=str(import_code),
                   DOCKER_TEST_EXPORT_CODE=str(export_code))
        package = Path(__file__).resolve().parents[1]
        command = ['bash',str(package/'run.sh'),'--sql',str(package/'demo/demo.sql.gz'),
                   '--uploads',str(package/'demo/demo-uploads.tar.gz'),'--output',str(root/'output with spaces')]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls, restored

    def test_imports_gzip_and_cleans_up_without_publishing_ports(self):
        result, calls, restored = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'CREATE TABLE experiments', restored.read_bytes())
        db_run = next(call for call in calls if call[0]=='run' and '-d' in call)
        self.assertNotIn('-p', db_run)
        self.assertNotIn('--publish', db_run)
        worker = next(call for call in calls if call[0]=='run' and '-d' not in call)
        self.assertTrue(any('target=/input/uploads,readonly' in part for part in worker))
        self.assertTrue(any(call[:2]==['rm','-fv'] for call in calls))
        self.assertTrue(any(call[:2]==['network','rm'] for call in calls))
        env_file = db_run[db_run.index('--env-file')+1]
        self.assertFalse(Path(env_file).exists())

    def test_import_failure_stops_export_and_still_removes_database(self):
        result, calls, _ = self.run_launcher(import_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(call[0]=='run' and '-d' not in call for call in calls))
        self.assertTrue(any(call[:2]==['rm','-fv'] for call in calls))

    def test_partial_export_retains_exit_code_and_cleans_up(self):
        result, calls, _ = self.run_launcher(export_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertIn('export-report.json', result.stderr)
        self.assertTrue(any(call[:2]==['rm','-fv'] for call in calls))


if __name__=='__main__':
    unittest.main()
