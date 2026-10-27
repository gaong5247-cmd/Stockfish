#!/usr/bin/env python3
"""Check startup, UCI responsiveness, perft and the Fishtest bench signature.

Uses only Python's standard library and works with native Windows pipes.
Run: python3 tests/fishtest_preflight.py src/stockfish --log preflight.log
Optional --base compares deterministic bench output for a non-functional patch.
An external-network build can use --uci-only --require-no-net from an empty
directory to check the worker's network-discovery command before installing NNUE.
These checks do not measure Elo or establish a statistically significant speedup.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import queue
import re
import subprocess
import sys
import threading
import time


# Selected directly from tests/perft.sh, including nonstandard castling layouts.
PERFT_CASES = (
    ("standard", False, "r7/4p3/5p1q/3P4/4pQ2/4pP2/6pp/R3K1kr w Q - 1 3", 5, 11609488),
    ("chess960_black", True,
     "rr6/2kpp3/1ppn2p1/p2b1q1p/P4P1P/1PNN2P1/2PP4/1K2R2R b E - 1 20", 2, 1438),
    ("chess960_white", True,
     "rr6/2kpp3/1ppn2p1/p2b1q1p/P4P1P/1PNN2P1/2PP4/1K2RR2 w E - 0 20", 3, 37340),
)
STARTPOS_MOVES = {
    *(f"{file}2{file}{rank}" for file in "abcdefgh" for rank in "34"),
    "b1a3", "b1c3", "g1f3", "g1h3",
}


class CheckFailed(RuntimeError):
    pass


def milliseconds(started):
    return round((time.perf_counter() - started) * 1000, 3)


def binary_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Engine:
    """Keep stdout drained independently of protocol deadlines on every OS."""

    def __init__(self, binary, timeout, log, arguments=()):
        self.timeout = timeout
        self.log = log
        self.transcript = []
        self.output = queue.Queue()
        self.started = time.perf_counter()
        self.process = subprocess.Popen(
            [str(binary), *arguments], cwd=binary.parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                line = line.rstrip("\r\n")
                self.transcript.append("< " + line)
                self.output.put(line)
        finally:
            self.output.put(None)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass  # Preserve the original failure if the engine closed a pipe.
        if self.log:
            self.log.write("\n".join(self.transcript) + "\n\n")
            self.log.flush()

    def send(self, command):
        self.transcript.append("> " + command)
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CheckFailed(f"Could not send {command!r}: {error}") from error

    def until(self, predicate, description, timeout=None):
        limit = self.timeout if timeout is None else timeout
        deadline = time.perf_counter() + limit
        lines = []
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise CheckFailed(f"Timeout after {limit}s waiting for {description}; {lines[-8:]}")
            try:
                line = self.output.get(timeout=remaining)
            except queue.Empty:
                raise CheckFailed(f"Timeout after {limit}s waiting for {description}; {lines[-8:]}")
            if line is None:
                raise CheckFailed(f"Engine exited before {description}; {lines[-8:]}")
            lines.append(line)
            if predicate(line):
                return lines

    def ready(self):
        started = time.perf_counter()
        self.send("isready")
        lines = self.until(lambda line: line == "readyok", "readyok")
        return milliseconds(started), lines

    def handshake(self):
        self.send("uci")
        self.until(lambda line: line == "uciok", "uciok")
        uciok_ms = milliseconds(self.started)
        self.ready()
        return {"process_to_uciok_ms": uciok_ms,
                "process_to_readyok_ms": milliseconds(self.started)}

    def finish(self, send_quit=True):
        started = time.perf_counter()
        if send_quit:
            self.send("quit")
        try:
            code = self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            raise CheckFailed("Engine did not terminate within the exit deadline") from error
        if code != 0:
            raise CheckFailed(f"Engine exited with status {code}")
        return milliseconds(started)


def check_bestmove(lines):
    move = lines[-1].split()[1]
    if move not in STARTPOS_MOVES:
        raise CheckFailed(f"Illegal or missing start-position bestmove: {move}")
    return move


def exercise_protocol(engine):
    engine.send("setoption name Threads value 2")
    engine.send("setoption name Hash value 32")
    options_ms, _ = engine.ready()
    engine.send("ucinewgame")
    engine.ready()
    engine.send("position startpos")
    engine.send("go nodes 1000")
    lines = engine.until(lambda line: line.startswith("bestmove "), "nodes-limited bestmove")
    bestmove = check_bestmove(lines)
    nodes = [int(match.group(1)) for line in lines
             if (match := re.search(r"\bnodes (\d+)\b", line))]
    if not nodes or max(nodes) < 1000:
        raise CheckFailed("Node-limited search did not report the requested work")

    engine.send("ucinewgame")
    engine.ready()
    engine.send("position startpos")
    engine.send("go infinite")
    engine.until(lambda line: line.startswith("info depth "), "active infinite search")
    ping_ms, ping_lines = engine.ready()
    if any(line.startswith("bestmove ") for line in ping_lines):
        raise CheckFailed("Search stopped while responding to isready")
    started = time.perf_counter()
    engine.send("stop")
    stopped = engine.until(lambda line: line.startswith("bestmove "), "bestmove after stop")
    stop_ms = milliseconds(started)
    check_bestmove(stopped)
    return {"threads": 2, "hash_mb": 32, "options_ready_ms": options_ms,
            "requested_nodes": 1000, "reported_nodes": max(nodes), "bestmove": bestmove,
            "search_isready_ms": ping_ms, "stop_to_bestmove_ms": stop_ms}


def check_perft(engine):
    results = []
    for name, chess960, fen, depth, expected in PERFT_CASES:
        engine.send(f"setoption name UCI_Chess960 value {str(chess960).lower()}")
        engine.ready()
        engine.send("position fen " + fen)
        started = time.perf_counter()
        engine.send(f"go perft {depth}")
        lines = engine.until(lambda line: re.match(r"Nodes searched\s*:", line), name + " perft")
        nodes = int(lines[-1].split(":", 1)[1])
        if nodes != expected:
            raise CheckFailed(f"{name} perft expected {expected}, got {nodes}")
        results.append({"name": name, "chess960": chess960, "depth": depth,
                        "nodes": nodes, "expected_nodes": expected,
                        "elapsed_ms": milliseconds(started)})
    return results


def run_bench(binary, expected, timeout, log):
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(binary), "bench"], cwd=binary.parent, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        if log:
            for output in (error.stdout, error.stderr):
                if output:
                    log.write(output.decode("utf-8", errors="replace")
                              if isinstance(output, bytes) else output)
            log.flush()
        raise CheckFailed(f"Bench exceeded {timeout}s") from error
    if log:
        log.write(f"BENCH {binary}\nSTDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}\n")
        log.flush()
    if completed.returncode != 0:
        raise CheckFailed(f"Bench exited with status {completed.returncode}: {completed.stderr[-2000:]}")
    signatures = re.findall(r"Nodes searched\s*:\s*(\d+)", completed.stderr)
    if len(signatures) != 1 or int(signatures[0]) != expected:
        raise CheckFailed(f"Bench expected {expected}, got {signatures}")
    stable_lines = [
        re.sub(r"\s+(?:time|nps|hashfull|tbhits)\s+\d+", "", line)
        for line in completed.stdout.splitlines()
        if line.startswith(("info depth ", "bestmove "))
    ]
    if not stable_lines:
        raise CheckFailed("Bench produced no search output to verify")
    stable_hash = hashlib.sha256("\n".join(stable_lines).encode()).hexdigest()
    timing = re.search(r"Total time \(ms\)\s*:\s*(\d+)", completed.stderr)
    return {"nodes_searched": int(signatures[0]), "expected_nodes": expected,
            "process_elapsed_ms": milliseconds(started),
            "reported_elapsed_ms": int(timing.group(1)) if timing else None,
            "stable_search_lines": len(stable_lines), "stable_search_sha256": stable_hash}, stable_lines


def run(args, report, log):
    binary = Path(args.engine).resolve(strict=True)
    report.update({"engine": str(binary), "binary_sha256": binary_hash(binary),
                   "platform": platform.platform(), "python": platform.python_version(),
                   "timeout_seconds": args.timeout, "bench_timeout_seconds": args.bench_timeout})
    if args.uci_only:
        if args.require_no_net and list(binary.parent.glob("*.nnue")):
            raise CheckFailed("Missing-network probe directory contains NNUE files")
        with Engine(binary, args.timeout, log, ("uci",)) as engine:
            lines = engine.until(lambda line: line == "uciok", "command-line uciok")
            report["process_to_uciok_ms"] = milliseconds(engine.started)
            nets = [match.group(1) for line in lines if (
                match := re.search(r"^option name EvalFile\w* type string default (nn-[a-f0-9]{12}\.nnue)$", line)
            )]
            if not nets:
                raise CheckFailed("Worker network-discovery command exposed no default NNUE filename")
            report["default_networks"] = nets
            report["exit_ms"] = engine.finish(send_quit=False)
            report["missing_net_directory_checked"] = args.require_no_net
        return

    report["startup"] = []
    for repetition in range(args.startup_repetitions):
        with Engine(binary, args.timeout, log) as engine:
            startup = engine.handshake()
            report["startup"].append(startup)
            if repetition == args.startup_repetitions - 1:
                report["uci"] = exercise_protocol(engine)
                report["perft"] = check_perft(engine)
            startup["quit_ms"] = engine.finish()

    report["bench"], stable_lines = run_bench(
        binary, args.expected_bench, args.bench_timeout, log)
    if args.base:
        base = Path(args.base).resolve(strict=True)
        baseline, base_lines = run_bench(base, args.expected_bench, args.bench_timeout, log)
        report["base"] = {"engine": str(base), "binary_sha256": binary_hash(base), "bench": baseline}
        if base_lines != stable_lines:
            raise CheckFailed("Baseline and candidate differ in deterministic bench score/PV/node output")
        report["base"]["deterministic_output_matches"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine")
    parser.add_argument("--expected-bench", type=int, default=2389370)
    parser.add_argument("--startup-repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Hard deadline per protocol response, in seconds (default: 5)")
    parser.add_argument("--bench-timeout", type=float, default=120.0)
    parser.add_argument("--base", help="Optional baseline binary for non-functional bench comparison")
    parser.add_argument("--log", type=Path, help="Write full engine transcripts and bench output")
    parser.add_argument("--uci-only", action="store_true", help="Only run the worker's ENGINE uci command")
    parser.add_argument("--require-no-net", action="store_true",
                        help="Assert the uci-only engine directory contains no NNUE files")
    args = parser.parse_args()
    if min(args.timeout, args.bench_timeout, args.startup_repetitions, args.expected_bench) <= 0:
        parser.error("Timeouts, repetitions and bench signature must be positive")
    if args.require_no_net and not args.uci_only:
        parser.error("--require-no-net requires --uci-only")

    report = {"status": "running", "mode": "uci-only" if args.uci_only else "full"}
    log = None
    try:
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            log = args.log.open("w", encoding="utf-8")
        run(args, report, log)
        report["status"] = "passed"
    except Exception as error:
        report.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
    finally:
        if log:
            log.close()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
