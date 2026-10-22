"""pi-env: the daemon that gives a host an execution environment on this machine over stdin and stdout.

Ported from pi's ``packages/env/daemon`` (Rust, v1.3.0) function by function; the modules follow the crate's
(``main``, ``frame``, ``errors``, ``decode``, ``fs``, ``scan``, ``window``, ``output``, ``exec``, ``watch``, ``list``,
``pty``, and ``system`` for its ``sys``). MISAKA fork: the daemon is Python, not a native binary. It runs on the remote machine with
Python 3.9 or later and nothing else: only the standard library, and only relative imports inside this package, because
``misaka.env.bundle`` ships it there as one zip application that runs as ``python3 <file> serve --token <hex>``.
"""
