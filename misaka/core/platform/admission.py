"""Host-wide worker admission limits shared by every LO and the net daemon."""
import os

import psutil

# A card is one model CLI process: a few hundred MiB resident while it streams. The host
# cap follows the memory that is actually free, but never below this floor -- a research
# node fans out eight cards at once, and a cap of two turned that into four serial rounds.
HOST_FLOOR = 4
HOST_CEILING = 12
PER_CARD_BYTES = 256 * 1024**2


def limits():
    """``(host_cap, per_sister_cap)``: cards running at once on this machine, and per Sister.

    The per-Sister cap defaults to the host cap. One roster with one Sister is the common
    shape, and a lower per-Sister default just left admission slots idle: the cards were
    hers to run and nothing else wanted the slots. Both stay overridable.
    """
    explicit = os.environ.get("MISAKA_MAX_CONCURRENT_SISTERS")
    if explicit is not None:
        host = max(1, int(explicit))
    else:
        by_memory = int(psutil.virtual_memory().available // PER_CARD_BYTES)
        host = max(HOST_FLOOR, min(HOST_CEILING, by_memory))
    per_sister = max(1, int(os.environ.get("MISAKA_MAX_CONCURRENT_PER_SISTER", host)))
    return host, min(host, per_sister)
