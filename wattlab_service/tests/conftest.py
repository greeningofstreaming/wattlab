"""
pytest configuration: make `wattlab_service/` importable as the source root
so tests can do `import carbon`, `import settings as cfg`, etc. without
having to mess with PYTHONPATH at the shell.

Run tests from anywhere with:
    pytest wattlab_service/tests/
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
