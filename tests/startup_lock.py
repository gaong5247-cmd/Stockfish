#!/usr/bin/env python3
"""Check UCI startup while another process holds the NNUE initialization lock.

Run on a Unix shared-memory build:
    python3 tests/startup_lock.py /path/to/stockfish

Use --expect-timeout against the unpatched engine to reproduce the old stall.
Only a uniquely copied test binary's shared-memory directory is touched.
"""

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time


def start_engine(binary, working_directory):
    process = subprocess.Popen(
        [str(binary)],
        cwd=working_directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    process.stdin.write(b"uci\nisready\n")
    process.stdin.flush()
    return process


def wait_for_line(process, prefix, timeout):
    started = time.monotonic()
    output = b""
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return False, (time.monotonic() - started) * 1000, output
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            continue
        data = os.read(process.stdout.fileno(), 65536)
        if not data:
            raise RuntimeError(f"Engine exited before {prefix!r}: {output.decode(errors='replace')}")
        output += data
        if any(line.startswith(prefix) for line in output.splitlines()):
            return True, (time.monotonic() - started) * 1000, output


def finish(process):
    try:
        process.communicate(b"quit\n", timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("Engine did not exit after quit")


def run(args):
    import fcntl

    original = Path(args.engine).resolve(strict=True)
    working_directory = (Path(args.working_directory).resolve(strict=True)
                         if args.working_directory else original.parent)
    if not working_directory.is_dir():
        raise RuntimeError("The working directory must be a directory")
    shared_root = Path(f"/tmp/stockfish-{os.getuid()}")
    lock_paths = []
    processes = []
    with tempfile.TemporaryDirectory(prefix="stockfish-startup-lock-") as temporary:
        binary = Path(temporary) / "stockfish-test"
        shutil.copy2(original, binary)
        try:
            warm = start_engine(binary, working_directory)
            processes.append(warm)
            ready, normal_ms, output = wait_for_line(warm, b"readyok", args.timeout)
            if not ready or b"uciok" not in output:
                raise RuntimeError("Normal engine startup did not complete the UCI handshake")

            sockets = list(shared_root.glob(f"sfshm_*/{warm.pid}.sock"))
            if len(sockets) != 1:
                raise RuntimeError(
                    f"Expected one default NNUE shared-memory socket, found {len(sockets)}; "
                    "this test requires a working Unix shared-memory backend"
                )
            lock_path = sockets[0].parent / "init_lock"
            lock_paths.append(lock_path)
            finish(warm)

            bestmove = None
            search_ms = None
            with lock_path.open("r+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                blocked = start_engine(binary, working_directory)
                processes.append(blocked)
                ready, held_lock_ms, output = wait_for_line(blocked, b"readyok", args.timeout)

                if args.expect_timeout:
                    if ready:
                        raise RuntimeError("Expected the baseline engine to stall, but it was ready")
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                    released, _, released_output = wait_for_line(blocked, b"readyok", args.timeout)
                    if not released or b"uciok" not in output + released_output:
                        raise RuntimeError("Engine did not recover after the held lock was released")
                elif not ready or b"uciok" not in output:
                    raise RuntimeError("Engine stalled while the NNUE initialization lock was held")
                else:
                    # Keep the lock held through a real search: readiness alone is insufficient.
                    blocked.stdin.write(b"position startpos\ngo nodes 1000\n")
                    blocked.stdin.flush()
                    searched, search_ms, search_output = wait_for_line(
                        blocked, b"bestmove ", args.timeout
                    )
                    if not searched:
                        raise RuntimeError("Engine did not return bestmove while the lock was held")
                    if b"info string Network replica 1: Local memory." not in search_output:
                        raise RuntimeError("Search did not report the expected local NNUE fallback")
                    bestmove = next(line.decode() for line in search_output.splitlines()
                                    if line.startswith(b"bestmove "))
                    if bestmove.split()[1] == "0000":
                        raise RuntimeError("Engine returned no move for the starting position")
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

            finish(blocked)
            print(json.dumps({
                "result": "baseline_stall_reproduced" if args.expect_timeout else "passed",
                "normal_ready_ms": round(normal_ms, 2),
                "held_lock_ready_ms": None if args.expect_timeout else round(held_lock_ms, 2),
                "held_lock_wait_ms": round(held_lock_ms, 2),
                "held_lock_bestmove": bestmove,
                "held_lock_search_ms": round(search_ms, 2) if search_ms is not None else None,
                "local_nnue_fallback_verified": not args.expect_timeout,
                "timeout_seconds": args.timeout,
            }, indent=2))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
            for lock_path in lock_paths:
                lock_path.unlink(missing_ok=True)
                try:
                    lock_path.parent.rmdir()
                except OSError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine")
    parser.add_argument("--working-directory", help="Directory containing external NNUE files")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--expect-timeout", action="store_true")
    arguments = parser.parse_args()
    if os.name != "posix":
        parser.error("This regression test requires Unix file locking and Unix shared memory")
    if arguments.timeout <= 1:
        parser.error("--timeout must be greater than the 1-second initialization-lock deadline")
    run(arguments)
