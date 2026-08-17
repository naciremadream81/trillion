"""
Loads .env once, deterministically, before any test module runs — same as
main.py and serve.py already do at real startup.

Without this, os.environ's contents during a test run depended on which
test files unittest happened to import first: several test files import
serve (see `grep -l "^import serve" tests/*.py`), and serve.py's own
module-level load_dotenv() call is a real side effect that only fires the
first time something imports it in the process. That made whether a given
test observed .env's values (e.g. a real BRAVE_SEARCH_API_KEY) depend on
test discovery order rather than on the code being tested — concretely, it
made tests/test_selfknowledge_drift.py's real-repo drift check pass or fail
depending on which other test files ran first in the same process, even
though the underlying question ("does context/self/trillion.md match this
machine's actual configured deployment") has one right answer regardless of
run order.

Loading it here, in the package __init__ that unittest imports before
collecting anything else, makes os.environ's starting state the same
whether a developer runs a single test file or `discover -s tests`.
"""

from dotenv import load_dotenv

load_dotenv()
