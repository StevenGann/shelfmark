import os
import tempfile

# Configure a scratch data directory before any test imports the app: the
# settings object is built at import time.
os.environ.setdefault("SHELFMARK_DATA_DIR", tempfile.mkdtemp(prefix="shelfmark-test-"))
