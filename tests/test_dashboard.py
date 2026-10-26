"""Regression checks for owned workload discovery."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from experiments import web_dashboard as dashboard


class DashboardChecks(unittest.TestCase):
    def sandbox(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.nginx_proc = Mock(pid=1234)
        sandbox.nginx_proc.poll.return_value = None
        return sandbox

    def test_worker_discovery_stays_with_owned_instance(self):
        sandbox = self.sandbox()
        snapshots = {1234: {"comm": "nginx"}, 1235: {"comm": "nginx"}, 1236: {"comm": "other"}}
        with patch.object(Path, "read_text", return_value="1235 1236"), \
                patch.object(dashboard, "process_snapshot", side_effect=snapshots.get, create=True):
            self.assertEqual(sandbox._get_nginx_worker_pids(), [1235])
        sandbox.nginx_proc.poll.return_value = 0
        self.assertEqual(sandbox._get_nginx_worker_pids(), [])



if __name__ == "__main__":
    unittest.main()
