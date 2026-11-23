"""Offline/isolated testing entry: live AI is available only in the shared worker.
Run the repository integration tests with temporary databases instead of a
standalone provider request. No Telegram messages are sent by this script.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

if __name__ == '__main__':
    import unittest
    suite=unittest.defaultTestLoader.loadTestsFromName('test_pipeline_integration')
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
