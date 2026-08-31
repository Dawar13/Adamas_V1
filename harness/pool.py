"""pool.py -- live emulator processes, reused between runs.

PROJECT-V2 §14.3. Measured on this project, roughly eight seconds of every
twenty-six second run is the emulator starting up, so the process is worth
keeping rather than the boot -- which is the opposite of what §14.1's model
predicts, and the reason this exists before the snapshot cache.

-----------------------------------------------------------------------------
THE ONE THING THIS MUST NOT DO
-----------------------------------------------------------------------------
A worker that has already run a test must produce, for the next test, EXACTLY
what a fresh process would have produced. Not nearly: byte for byte, in the
event log. A pool that contaminates one test with the last one is worse than no
pool, because the contamination is invisible -- the verdict still looks like a
verdict, and the only evidence is a log nobody is comparing.

So `Clear` between tests, and the equivalence harness over the result. This
module is the mechanism; harness/equivalence.py is the check, and neither
is allowed to vouch for the other.

-----------------------------------------------------------------------------
THE PROTOCOL, AND THE THREE TRAPS IN IT
-----------------------------------------------------------------------------
Renode's `-P <port>` listens for Monitor commands on a socket. Driving it
looked simple and was not:

  1. THE SOCKET ECHOES EVERY COMMAND. A sentinel that appears literally in the
     command matches its own echo, and the driver believes a command finished
     before it started. The token here is assembled from two halves inside the
     command, so the echo cannot contain it.

  2. THE GREETING CARRIES TELNET NEGOTIATION BYTES. Stripped, or they end up in
     the console text a run records as its own output.

  3. A COMPILED SCRIPT ENDS WITH `quit`, WHICH KILLS THE WORKER. The trailing
     quit is removed from what is SENT -- never from the file. The .resc on
     disk stays a standalone script that reproduces the run on its own, which
     is what the reproduction note promises.

  4. THE MONITOR SERVES EXACTLY ONE CLIENT. A second connection is accepted at
     the TCP level and then never serviced -- it does not refuse, it hangs. So
     the POOL MUST NOT HOLD A CONNECTION to a worker it lends out: the borrower
     would wait sixty seconds and report a timeout that looks exactly like a
     hang in the firmware. Measured, after it did precisely that: the pool
     spawns processes and checks readiness by connecting and closing again.

     The same constraint means one test per worker at a time, which is what a
     scheduler wants anyway.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
No node, board, message or peripheral name appears here. This module knows
about processes, sockets and files.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

#: The sentinel, and the command that produces it. The command is written so
#: that its own echo does not contain the token -- see trap 1.
TOKEN = "BENCHWORKERDONE"
SENTINEL_CMD = "python \"print 'BENCHWORKER' + 'DONE'\""

#: How long to wait for a worker to accept a connection before giving up on it.
START_TIMEOUT = 60.0

#: How long a single receive may block before we look again at the clock.
POLL = 1.0


#: Included once per worker, so `reset` can ask rather than assume. IronPython
#: 2, like everything else that runs inside the emulator.
STATE_PROBE = """
def mc_worker_state(unused):
    emu = emulationManager.CurrentEmulation
    us = int(emu.MasterTimeSource.ElapsedVirtualTime.Ticks) / 1000
    machines = len([n for n in emu.Names])
    print 'BENCHWORKER' + 'STATE us=%d machines=%d' % (us, machines)
"""


class WorkerError(Exception):
    """The worker could not be started, or died while holding a run."""


def _strip_iac(data: bytes) -> bytes:
    """Telnet negotiation out, text in (trap 2)."""
    out = bytearray()
    index = 0
    while index < len(data):
        if data[index] == 0xFF and index + 2 < len(data):
            index += 3
            continue
        out.append(data[index])
        index += 1
    return bytes(out)


def strip_trailing_quit(script: Path) -> str:
    """A compiled script as a worker may run it: everything but the `quit`.

    Only the trailing quit is dropped, and only from the text that is sent. A
    `quit` in the middle would be a script doing something this does not
    understand, so it is left alone and the worker will die on it -- loudly,
    which is the right way to find out.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    while lines and lines[-1].strip() in ("", "quit"):
        if lines[-1].strip() == "quit":
            lines.pop()
            break
        lines.pop()
    return "\n".join(lines)


class Worker:
    """One live emulator process, driven over its Monitor socket."""

    def __init__(self, renode: str, port: int, env=None, extra_args=(),
                 log_path=None, connect: bool = True):
        self.port = port
        self.renode = renode
        self.command = [renode, "-P", str(port), "--disable-xwt", "--plain"]
        self.command.extend(extra_args)

        # NOT a pipe. OBSERVED: with stdout captured by a pipe that nothing
        # drains, the emulator fills the 64 KB buffer part way through a run,
        # blocks on its next write, and stops answering the monitor socket.
        # The run then dies of a timeout that looks exactly like a hang in the
        # firmware, twenty seconds after everything appeared healthy.
        self.log_path = Path(log_path) if log_path else None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(self.log_path, "wb")
            sink = self._log
        else:
            self._log = None
            sink = subprocess.DEVNULL
        self.proc = subprocess.Popen(
            self.command,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env if env is not None else os.environ.copy(),
        )
        self.sock = None
        self.buf = ""
        self._probe_ready = False
        if connect:
            self._connect()
        else:
            self._wait_until_listening()

    @classmethod
    def attach_existing(cls, host: str, port: int) -> "Worker":
        """Talk to a worker this process did not start.

        The pool owns the processes; a single run borrows one. A borrowed
        worker is never killed by the borrower -- the run that started it
        decides when it dies, or the whole pool would end with the first test.
        """
        self = cls.__new__(cls)
        self.port = port
        self.renode = None
        self.command = ["<attached to %s:%d>" % (host, port)]
        self.proc = None
        self._log = None
        self.log_path = None
        self.sock = None
        self.buf = ""
        self._probe_ready = False
        try:
            self.sock = socket.create_connection((host, port), timeout=5)
        except OSError as exc:
            raise WorkerError("nothing is listening on %s:%d (%s)"
                              % (host, port, exc))
        self.sock.settimeout(POLL)
        self._read_for(0.5)
        return self

    # -- lifecycle ---------------------------------------------------------

    def _wait_until_listening(self):
        """Ready, without taking the one connection this worker can serve.

        The pool owns processes; borrowers own connections. Holding a socket
        here would starve the borrower (trap 4).
        """
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise WorkerError(
                    "the emulator exited with %s before it listened on port %d"
                    % (self.proc.returncode, self.port)
                )
            try:
                probe = socket.create_connection(("127.0.0.1", self.port),
                                                 timeout=2)
                probe.close()
                return
            except OSError:
                time.sleep(0.25)
        self.kill()
        raise WorkerError("nothing listened on port %d within %.0f s"
                          % (self.port, START_TIMEOUT))

    def _connect(self):
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise WorkerError(
                    "the emulator exited with %s before accepting a connection "
                    "on port %d" % (self.proc.returncode, self.port)
                )
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port),
                                                     timeout=2)
                break
            except OSError:
                time.sleep(0.25)
        if self.sock is None:
            self.kill()
            raise WorkerError("no worker accepted a connection on port %d "
                              "within %.0f s" % (self.port, START_TIMEOUT))
        self.sock.settimeout(POLL)
        self._read_for(1.0)          # the greeting, and its negotiation bytes

    @property
    def alive(self) -> bool:
        # An attached worker has no process handle here: its liveness is the
        # socket's, and a dead socket surfaces as a send or receive failure.
        return True if self.proc is None else self.proc.poll() is None

    def kill(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # -- talking to it -----------------------------------------------------

    def _read_for(self, seconds: float) -> str:
        end = time.time() + seconds
        got = ""
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            except OSError as exc:
                raise WorkerError("the worker's socket failed: %s" % exc)
            if not chunk:
                # EOF on a stream socket means the other end is gone. Treated
                # as silence, this cost 120 seconds per crash: the borrower sat
                # out its whole timeout after the worker had already died, and
                # reported a timeout rather than a death. A killed worker is
                # now noticed within a poll.
                raise WorkerError(
                    "the worker closed the connection; it is no longer running"
                )
            got += _strip_iac(chunk).decode("utf-8", "replace")
        self.buf += got
        return got

    def _send(self, line: str):
        try:
            self.sock.sendall((line + "\n").encode("utf-8"))
        except OSError as exc:
            raise WorkerError("could not send to the worker: %s" % exc)

    def command_lines(self, text: str, timeout: float) -> str:
        """Send monitor text, and return everything it printed.

        Completion is a sentinel, not a prompt: prompts change between versions
        and appear inside output, and a driver that guesses wrong reports a run
        as finished while it is still going.
        """
        self.buf = ""
        self._send(text)
        self._send(SENTINEL_CMD)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if TOKEN in self.buf:
                return self.buf
            if not self.alive:
                raise WorkerError(
                    "the worker died while running a script (exit %s)"
                    % (self.proc.returncode if self.proc else "unknown")
                )
            self._read_for(POLL)
        raise WorkerError(
            "the worker produced no sentinel within %.0f s. A run that did not "
            "finish has no result, and the worker is not reused." % timeout
        )

    def run_resc(self, script: Path, timeout: float) -> str:
        """Run one compiled script, without letting it quit the process."""
        return self.command_lines(strip_trailing_quit(script), timeout)

    #: Printed by the cleanliness check. The command that triggers it does not
    #: contain the token, so the socket's echo cannot be read as the answer.
    CLEAN_TOKEN = "BENCHWORKERSTATE"
    CLEAN_CMD = 'worker_state ""'

    def ensure_state_probe(self, path: Path, as_seen_by_worker=None,
                           timeout: float = 60.0):
        """Teach this worker to describe its own state.

        A file rather than a one-line `python "..."` command: the monitor, the
        socket and IronPython each want their own quoting, and a nested
        one-liner that almost works is how a cleanliness check ends up
        reporting on nothing. The toolkit is included the same way.
        """
        if self._probe_ready:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STATE_PROBE, encoding="utf-8", newline="\n")
        where = as_seen_by_worker or path.as_posix()
        self.command_lines("include @%s" % where, timeout)
        self._probe_ready = True

    def reset(self, timeout: float = 60.0) -> str:
        """Back to an empty emulation, and CHECKED, ready for the next test.

        `Clear` disposes every machine and returns virtual time to zero. Both
        halves matter and neither is assumed:

        OBSERVED, and it is why this method verifies rather than trusts. A
        first version of the warm path never called Clear at all. The second
        run in a worker then started at the first run's virtual time, its
        event log came out as both runs concatenated, and every assertion was
        armed at an instant from the previous test -- while each run still
        reported a verdict that looked exactly like a verdict. The equivalence
        harness caught it; nothing else would have.

        So a worker states its own state, and a worker that is not empty is
        refused rather than reused.
        """
        self.command_lines("Clear", timeout)
        text = self.command_lines(self.CLEAN_CMD, timeout)
        for line in text.splitlines():
            if self.CLEAN_TOKEN not in line:
                continue
            tail = line.split(self.CLEAN_TOKEN, 1)[1]
            if "us=0" in tail and "machines=0" in tail:
                return text
            raise WorkerError(
                "the worker is not clean after Clear (%s). A test started on a "
                "dirty worker inherits the last one's virtual time and its "
                "machines, and reports a verdict anyway." % tail.strip()
            )
        raise WorkerError(
            "the worker would not say whether it is clean, so it is not reused."
        )


# ---------------------------------------------------------------------------
# the pool
# ---------------------------------------------------------------------------


#: What the engine returns when the emulator holding a run died. Spelled here
#: rather than imported, so this module stays independent of the engine it
#: launches; a test pins the two together.
EXIT_CRASHED = 5


class Pool:
    """N live emulators, and the policy for what happens when one dies.

    PROJECT-V2 §14.3: "A worker that crashes is replaced; its test is re-queued
    ONCE and then reported as exit 5, crashed -- never as a firmware failure."

    Both halves of that sentence are load-bearing and they pull in opposite
    directions:

      re-queue once   a worker can die for reasons that have nothing to do
                      with the test -- the host reclaimed memory, the process
                      was killed. Reporting the first such death as a result
                      would blame the firmware for our infrastructure.

      ONCE            retrying forever turns a test that reliably kills the
                      emulator into a run that never finishes, and a test that
                      kills emulators is a finding, not a nuisance.
    """

    def __init__(self, renode: str, size: int = 1, first_port: int = 45600,
                 env=None, log_dir=None):
        self.log_dir = log_dir
        self.renode = renode
        self.size = max(1, int(size))
        self.first_port = first_port
        self.env = env
        self.workers = []
        self.replacements = 0

    def start(self):
        for index in range(self.size):
            self.workers.append(self._spawn(self.first_port + index))
        return self

    def _spawn(self, port: int) -> Worker:
        log = None
        if self.log_dir is not None:
            log = Path(self.log_dir) / ("worker-%d.log" % port)
        return Worker(self.renode, port, env=self.env, log_path=log,
                      connect=False)

    def endpoint(self, index: int = 0) -> str:
        return "127.0.0.1:%d" % self.workers[index].port

    def replace(self, index: int = 0) -> str:
        """Kill whatever is there and put a fresh worker on the same port."""
        old = self.workers[index]
        port = old.port
        old.kill()
        # The port takes a moment to come free; a replacement that cannot bind
        # is a replacement that never arrives.
        time.sleep(1.0)
        self.workers[index] = self._spawn(port)
        self.replacements += 1
        return self.endpoint(index)

    def stop(self):
        for worker in self.workers:
            worker.kill()
        self.workers = []

    # -- the policy --------------------------------------------------------

    def run_once(self, run_command, index: int = 0):
        """Run one test on a worker, replacing it and retrying exactly once.

        `run_command` takes an endpoint and returns the engine's exit code, so
        this class never has to know what a scenario is.

        Returns (exit_code, attempts). An exit code of EXIT_CRASHED after two
        attempts is the honest answer: this test did not run, twice, and that
        is not something the firmware did.
        """
        code = run_command(self.endpoint(index))
        if code != EXIT_CRASHED:
            return code, 1

        # One replacement, one retry. Not a loop.
        endpoint = self.replace(index)
        code = run_command(endpoint)
        return code, 2

    def run_tests(self, items, run_command, on_result=None):
        """Dispatch items across the workers, one test per worker at a time.

        One at a time per worker is not a scheduling choice: the Monitor serves
        a single client (trap 4), so a worker running two tests at once is a
        worker running neither.

        Each worker index is owned by exactly one thread, so a replacement
        never races another thread's view of which process it is talking to.
        """
        pending = list(items)
        results = []
        lock = threading.Lock()

        def take():
            with lock:
                return pending.pop(0) if pending else None

        def loop(index):
            while True:
                item = take()
                if item is None:
                    return
                code, attempts = self.run_once(
                    lambda endpoint: run_command(item, endpoint), index)
                with lock:
                    results.append((item, code, attempts))
                    if on_result is not None:
                        on_result(item, code, attempts)

        threads = [threading.Thread(target=loop, args=(i,), daemon=True)
                   for i in range(len(self.workers))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results
