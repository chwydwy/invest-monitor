import unittest
from unittest.mock import patch

import deploy_pi


OLD = "a" * 40
NEW = "b" * 40


class DeployPiTest(unittest.TestCase):
    def test_secret_and_runtime_paths_rejected(self):
        for path in ("dart_monitor/config.json", "news_monitor/config_news.json",
                     "dashboard_outbox.sqlite3", "private/monitor.token", ".env.production"):
            self.assertTrue(deploy_pi.is_secret(path))
            with self.assertRaises(deploy_pi.DeployError):
                deploy_pi.validate_paths([path])
        self.assertFalse(deploy_pi.is_secret("dart_monitor/dart.py"))
        with self.assertRaisesRegex(deploy_pi.DeployError, "unmapped"):
            deploy_pi.validate_service_scope(["stock_monitor/price.py"])

    def test_remote_program_syntax_and_sha_validation(self):
        compile(deploy_pi.REMOTE_PROGRAM, "<remote deployment>", "exec")
        with self.assertRaises(deploy_pi.DeployError):
            deploy_pi.remote("apply", "invalid", NEW)

    @patch.object(deploy_pi, "command")
    @patch.object(deploy_pi, "git")
    def test_local_check_rejects_unstaged_changes(self, git, command):
        git.side_effect = lambda *args: "main" if args[0] == "branch" else " M news_monitor/news.py"
        with self.assertRaisesRegex(deploy_pi.DeployError, "unstaged"):
            deploy_pi.local_check()
        command.assert_not_called()

    @patch.object(deploy_pi, "remote")
    @patch.object(deploy_pi, "remote_head", return_value=OLD)
    @patch.object(deploy_pi, "git")
    @patch.object(deploy_pi, "local_check", return_value=[])
    def test_check_is_read_only(self, local_check, git, remote_head, remote):
        git.side_effect = lambda *args: NEW if args[0] == "rev-parse" else ""
        remote.return_value = {"head": OLD, "services": {"dart.service": "active", "news.service": "active"}}
        self.assertEqual(deploy_pi.main(["check"]), 0)
        self.assertEqual([call.args[0] for call in git.call_args_list], ["rev-parse", "diff"])
        remote.assert_called_once_with("check", OLD, NEW)

    @patch.object(deploy_pi, "remote")
    @patch.object(deploy_pi, "remote_head", return_value=OLD)
    @patch.object(deploy_pi, "git")
    @patch.object(deploy_pi, "local_check", return_value=["news_monitor/news.py"])
    def test_apply_publishes_exact_commit(self, local_check, git, remote_head, remote):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "rev-parse":
                return OLD if calls.count(("rev-parse", "HEAD")) == 1 else NEW
            if args[0] == "ls-remote":
                return f"{OLD if calls.count(args) == 1 else NEW}\trefs/heads/main"
            return ""

        git.side_effect = fake_git
        remote.return_value = {"head": OLD, "applied": NEW,
                               "restart_authorized": {"dart.service": True, "news.service": True}}
        self.assertEqual(deploy_pi.main(["apply", "--message", "Update news"]), 0)
        self.assertLess(calls.index(("commit", "-m", "Update news")), calls.index(("push", "origin", "main")))
        self.assertEqual(remote.call_args_list[-1].args, ("apply", OLD, NEW))

    @patch.object(deploy_pi, "remote", return_value={"restart_authorized": {"news.service": False}})
    @patch.object(deploy_pi, "remote_head", return_value=OLD)
    @patch.object(deploy_pi, "git")
    @patch.object(deploy_pi, "local_check", return_value=["news_monitor/news.py"])
    def test_missing_restart_permission_stops_before_commit(self, local_check, git, remote_head, remote):
        git.side_effect = lambda *args: NEW if args[0] == "rev-parse" else ""
        with self.assertRaisesRegex(deploy_pi.DeployError, "restart permission"):
            deploy_pi.main(["apply", "--message", "Update news"])
        self.assertNotIn("commit", [call.args[0] for call in git.call_args_list])


if __name__ == "__main__":
    unittest.main()
