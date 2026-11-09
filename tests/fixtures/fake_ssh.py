"""A stand-in for ``ssh`` in tests: it skips ssh's options up to ``--`` and the host, joins the rest as ssh does into one
remote command, and runs that with ``sh -c`` on this machine, with stdin, stdout and stderr passed through. The
environment variable ``FAKE_SSH_HOME`` becomes the remote ``HOME``."""

import os
import sys

args = sys.argv[1:]
index = args.index("--")
command = " ".join(args[index + 2 :])
if "FAKE_SSH_HOME" in os.environ:
    os.environ["HOME"] = os.environ["FAKE_SSH_HOME"]
os.execvp("sh", ["sh", "-c", command])
