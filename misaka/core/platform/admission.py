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


def occupied(con):
    """Cards consuming a host slot, including accepted headless workers still settling.

    The current attempt already records its process identity. Inspect only that
    attempt, not historical runs; permanent interactive card panes are not tails.
    """
    from misaka.core.platform import processes
    from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY

    taken = dict(con.execute(
        "SELECT id,assignee FROM tasks WHERE status='running' AND claim_lock IS NOT NULL"))
    rows = con.execute(
        "SELECT t.id,t.assignee,r.pid,r.process_identity FROM tasks t "
        "JOIN task_runs r ON r.id=t.current_run_id AND r.generation=t.generation "
        "WHERE t.status IN ('done','review') AND r.phase='worker' "
        "AND r.status IN ('done','submitted') AND r.pid IS NOT NULL"
    ).fetchall()
    if not rows:
        return taken
    live_pids = set(psutil.pids())
    for row in rows:
        identity = str(row["process_identity"] or "")
        if row["pid"] not in live_pids or not identity.startswith(PROCESS_GROUP_IDENTITY):
            continue
        if not processes.identity_is_alive(row["pid"], identity[len(PROCESS_GROUP_IDENTITY):]):
            continue
        try:
            argv = psutil.Process(row["pid"]).cmdline()
        except psutil.Error:
            continue
        # Both the research worker wrapper and its durable Sister child may
        # own the attempt. A card-shell TUI deliberately lives beyond its turn.
        module = next((argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-m"), None)
        if module == "misaka.cli.subagent_child" or (
            module == "misaka.cli.research_node" and "--run-card" in argv
        ):
            taken[row["id"]] = row["assignee"]
    return taken
