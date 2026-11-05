"""
Test-session defaults.

APP_ENV defaults to the strict production posture when unset
(src/agent/runtime_env.py), so the suite opts into development mode here,
before any test module imports src/api/server.py. Tests that exercise the
production posture build their own settings explicitly.
"""

import os

os.environ.setdefault("APP_ENV", "dev")
