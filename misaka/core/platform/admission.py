"""Host-wide worker admission limits shared by every LO and the net daemon."""
import os

import psutil


def limits():
    explicit = os.environ.get("MISAKA_MAX_CONCURRENT_SISTERS")
    if explicit is not None:
        host = max(1, int(explicit))
    else:
        # One active model CLI is budgeted at 512 MiB; stay deliberately small.
        host = max(1, min(8, int(psutil.virtual_memory().available // (512 * 1024**2))))
    per_sister = max(
        1,
        int(os.environ.get("MISAKA_MAX_CONCURRENT_PER_SISTER", min(2, host))),
    )
    return host, min(host, per_sister)
