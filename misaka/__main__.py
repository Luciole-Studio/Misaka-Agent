"""MISAKA command-line entry point."""
import os
import sys

from misaka.cli.app import main as _run


def main(argv=None):
    """Process entry: a reader that hangs up (``misaka ... | head``) ends the run quietly instead of
    with a traceback. SIGPIPE stays ignored (Python's default) so a tool's child exiting early raises
    ``BrokenPipeError`` where it can be handled rather than killing the whole process."""
    try:
        return _run(argv)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # let the interpreter's final flush succeed
        return 141


if __name__ == "__main__":
    sys.exit(main())
