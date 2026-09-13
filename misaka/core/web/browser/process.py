"""Awaited browser helper processes; inherited daemon FDs never hold a PIPE open."""
import asyncio
import os
import signal
import subprocess
import tempfile

from misaka.utils.async_lifecycle import run_in_thread, settle


async def terminate(proc):
    if proc.returncode is None:
        try:
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def command(argv, env, *, cwd, timeout=30, input_bytes=None):
    # Temp files also bound the amount loaded into RAM; raw output remains private.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc, cancelled = await settle(asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, env=env, cwd=cwd, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=os.name == 'posix')))
        if cancelled is not None:
            await settle(asyncio.create_task(terminate(proc)))
            raise cancelled
        try:
            async with asyncio.timeout(timeout):
                if input_bytes is not None:
                    proc.stdin.write(input_bytes)
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                await proc.wait()
            def read():
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(4 * 1024 * 1024), stderr.read(4000)
                if stdout.read(1):
                    raise ValueError('Browser output exceeds 4 MiB; write large results to a workspace file')
                return out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
            out, err = await run_in_thread(read)
            return proc.returncode, out, err
        finally:
            _, cancelled = await settle(asyncio.create_task(terminate(proc)))
            if cancelled is not None:
                raise cancelled
