import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LECTERN_DATA_DIR", tempfile.mkdtemp(prefix="lectern-test-"))
os.environ.setdefault("LECTERN_API_KEY", "test-key-0123456789abcdef")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-a-real-key")
