# tests/conftest.py
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_proj")
