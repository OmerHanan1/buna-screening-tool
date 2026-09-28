"""Fail-closed kernel checks run by every public replica before HTTP startup."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

from buna.public_worker import isolate


def verify_kernel():
    with tempfile.TemporaryDirectory(prefix="kernel-check-") as temp:
        folder = Path(temp)
        folder.chmod(0o711)
        private = folder / "private"
        private.write_text("synthetic private sentinel")
        private.chmod(0o600)
        result = subprocess.run([sys.executable, "-m", "buna.public_selftest", str(private)],
                                capture_output=True, timeout=15, check=False,
                                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": "/app"})
        if result.returncode != 0 or result.stdout.strip() != b"isolated":
            raise RuntimeError("Public worker kernel isolation self-test failed; refusing HTTP startup.")


def denied(operation):
    try:
        operation()
    except PermissionError:
        return
    raise RuntimeError("Required isolation denied-operation check failed.")


if __name__ == "__main__":
    isolate(60001)
    denied(lambda: Path(sys.argv[1]).read_bytes())
    denied(lambda: socket.socket())
    denied(os.fork)
    denied(os.setsid)
    denied(lambda: os.setpgid(0, 0))
    denied(lambda: os.execv("/bin/true", ["/bin/true"]))
    print("isolated")
