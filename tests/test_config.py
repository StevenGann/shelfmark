import os
import subprocess
import sys


def test_settings_ignores_kubernetes_service_links():
    """A Service named shelfmark injects SHELFMARK_PORT=tcp://... into the pod.

    A settings field called ``port`` would try to parse that as an integer and
    take the process down at import. Run in a subprocess so the injected
    environment cannot leak into the rest of the suite.
    """
    env = dict(
        os.environ,
        SHELFMARK_PORT="tcp://10.43.60.49:80",
        SHELFMARK_SERVICE_HOST="10.43.60.49",
        SHELFMARK_PORT_80_TCP="tcp://10.43.60.49:80",
        SHELFMARK_DATA_DIR="/tmp/shelfmark-config-test",
    )
    code = "import app.config as c; print(c.settings.listen_port, c.settings.data_dir)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.split() == ["8080", "/tmp/shelfmark-config-test"]
