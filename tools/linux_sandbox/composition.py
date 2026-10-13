"""Run a disposable, native Linux composition experiment for issue #60.

This is an opt-in acceptance tool, not the Sandbox runner. It executes only the
repository's static C fixture. It never installs software, configures systemd,
enables lingering, changes sysctls, or requests privilege. A successful experiment
does NOT establish a supported Python runtime or a production isolation baseline.

Usage: python tools/linux_sandbox/composition.py --output evidence.json
Requires rootless native Linux, an existing systemd >=257 user manager, delegated
cgroup v2 cpu/memory/pids, unshare, mount, bwrap, cc, static libc, pkg-config and
the libsystemd/libseccomp development files. Missing enforcement fails; no skips.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


SCRIPT = Path(__file__).resolve()
SOURCE = SCRIPT.with_name("fixture.c")
CONTROLLERS = {"cpu", "memory", "pids"}
GAPS = [
    "Not a supported or tested Python runtime profile.",
    "No verified Artifact/dependency immutable-byte binding is implemented here.",
    "The seccomp filter is specific to this trusted fixture, not a production policy.",
    "WSL2 and native Windows are unsupported by this experiment.",
    "Issues #61 and #62 and the complete #60 acceptance contract remain separate gates.",
]


class ProbeFailure(RuntimeError):
    """A prerequisite, assertion, or cleanup failed; never retry unsandboxed."""


def run(command: list[str], *, timeout: float = 20, cwd: str | None = None,
        pass_fds: tuple[int, ...] = ()) -> str:
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False, cwd=cwd, pass_fds=pass_fds,
    )
    if result.returncode:
        raise ProbeFailure(
            f"{Path(command[0]).name} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def prerequisites() -> dict[str, object]:
    if sys.platform != "linux":
        raise ProbeFailure("native Linux is required; this process cannot run the composition experiment")
    release = platform.release()
    if (any(marker in (release + platform.version()).lower() for marker in ("microsoft", "wsl"))
            or "WSL_INTEROP" in os.environ or "WSL_DISTRO_NAME" in os.environ
            or Path("/run/WSL").exists()):
        raise ProbeFailure("WSL2 needs a separate acceptance profile; this experiment requires native Linux")
    if os.geteuid() == 0:
        raise ProbeFailure("run as an ordinary user; root cannot prove rootless delegation")
    if not hasattr(os, "pidfd_open"):
        raise ProbeFailure("Python pidfd_open support is required for process identity-safe cleanup evidence")
    cgroup = Path("/sys/fs/cgroup")
    if not (cgroup / "cgroup.controllers").is_file():
        raise ProbeFailure("the unified cgroup v2 hierarchy is required")
    tools: dict[str, str] = {}
    for name in ("bwrap", "systemctl", "unshare", "mount", "cc", "pkg-config", "readelf"):
        found = shutil.which(name, path="/usr/bin:/bin")
        if not found:
            raise ProbeFailure(f"required executable is missing: {name}")
        tools[name] = str(Path(found).resolve())
    versions = {name: run([tools[name], "--version"]).splitlines()[0]
                for name in ("bwrap", "systemctl", "unshare", "mount", "cc")}
    manager = run([tools["systemctl"], "--user", "show", "--property=Version", "--value"])
    match = re.match(r"(\d+)", manager)
    if not match or int(match[1]) < 257:
        raise ProbeFailure(f"existing systemd user manager >=257 is required for ExtraFileDescriptors: {manager!r}")
    versions["user_manager"] = manager
    for library in ("libsystemd", "libseccomp"):
        versions[library] = run([tools["pkg-config"], "--modversion", library])
    return {"kernel": release, "machine": platform.machine(), "python": sys.version,
            "uid": os.getuid(), "tools": tools, "versions": versions}


def compile_fixture(work: Path, tools: dict[str, str]) -> None:
    flags = shlex.split(run([tools["pkg-config"], "--cflags", "--libs", "libsystemd", "libseccomp"]))
    common = [tools["cc"], "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(SOURCE)]
    run([*common, "-o", str(work / "helper"), *flags], timeout=60)
    run([*common, "-DPAYLOAD", "-static", "-o", str(work / "fixture")], timeout=60)
    segments = run([tools["readelf"], "-l", str(work / "fixture")])
    dynamic = run([tools["readelf"], "-d", str(work / "fixture")])
    if "INTERP" in segments or "(NEEDED)" in dynamic:
        raise ProbeFailure("the fixture must be static and have no interpreter or dependency closure")
    os.chmod(work / "fixture", 0o500)
    run([str(work / "helper"), "--filter", str(work / "filter.bpf")])
    (work / "tools.json").write_text(json.dumps(tools), encoding="utf-8")


def emit(fd: int, event: dict[str, object]) -> None:
    data = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(data) > 4096:
        raise ProbeFailure("internal evidence exceeds atomic pipe size")
    if os.write(fd, data) != len(data):
        raise ProbeFailure("short evidence write")


def close_except(keep: set[int]) -> None:
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass  # The directory descriptor from listdir is already closed.


def own_cgroup() -> Path:
    entries = Path("/proc/self/cgroup").read_text().splitlines()
    unified = [line[3:] for line in entries if line.startswith("0::/")]
    if len(unified) != 1 or ".." in Path(unified[0]).parts:
        raise ProbeFailure("cannot identify own unified cgroup")
    return Path("/sys/fs/cgroup") / unified[0].lstrip("/")


def supervisor(work: Path, token: str) -> int:
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    if (os.environ.get("LISTEN_PID") != str(os.getpid()) or
            os.environ.get("LISTEN_FDS") != "4" or len(names) != 4 or
            set(names) != {"broker-request", "broker-reply", "lease", "evidence"}):
        raise ProbeFailure("systemd did not deliver the four named descriptors")
    fds = dict(zip(names, range(3, 7)))
    evidence = fds["evidence"]
    close_except(set(fds.values()))
    child: subprocess.Popen[bytes] | None = None
    try:
        current = own_cgroup()
        unit = f"penhin-composition-{token}.service"
        if current.name != "supervisor" or current.parent.name != unit:
            raise ProbeFailure("DelegateSubgroup did not place MainPID in supervisor/")
        delegated = current.parent
        available = set((delegated / "cgroup.controllers").read_text().split())
        if not CONTROLLERS <= available:
            raise ProbeFailure(f"required controllers were not delegated: {sorted(available)}")
        (delegated / "cgroup.subtree_control").write_text("+cpu +memory +pids")
        if not CONTROLLERS <= set((delegated / "cgroup.subtree_control").read_text().split()):
            raise ProbeFailure("delegated controllers could not be enabled")
        payload = delegated / "payload"
        payload.mkdir()
        limits = {"memory.max": "67108864", "memory.swap.max": "0",
                  "pids.max": "32", "cpu.max": "100000 100000"}
        for name, value in limits.items():
            (payload / name).write_text(value)
            if (payload / name).read_text().strip() != value:
                raise ProbeFailure(f"controller did not retain {name}={value}")
        if not (payload / "cgroup.kill").is_file():
            raise ProbeFailure("cgroup.kill is required for recursive teardown")
        emit(evidence, {"event": "delegated", "main_pid": os.getpid(),
                        "cgroup": str(delegated), "payload_cgroup": str(payload),
                        "controllers": sorted(available), "limits": limits})
        tools = json.loads((work / "tools.json").read_text())
        child = subprocess.Popen(
            [tools["unshare"], "--user", "--map-root-user", "--mount", "--propagation", "private",
             sys.executable, "-I", "-S", str(SCRIPT), "_mount", str(work), token,
             str(payload), str(fds["broker-request"]), str(fds["broker-reply"]), str(evidence)],
            pass_fds=(fds["broker-request"], fds["broker-reply"], evidence),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(fds["broker-request"])
        os.close(fds["broker-reply"])
        while child.poll() is None:
            readable, _, _ = select.select([fds["lease"]], [], [], 0.1)
            if readable:
                # The core owns the only write end. No guest inherits this lease.
                if os.read(fds["lease"], 1):
                    raise ProbeFailure("unexpected bytes on lifetime-only lease")
                emit(evidence, {"event": "lease_eof", "main_pid": os.getpid()})
                return 0  # systemd sees MainPID exit and kills remaining cgroup processes.
        raise ProbeFailure(f"fixture launcher exited unexpectedly: {child.returncode}")
    except Exception as error:
        emit(evidence, {"event": "failure", "reason": str(error)})
        return 1


def mount_fixture(work: Path, token: str, payload: Path, request: int, reply: int, evidence: int) -> int:
    try:
        # This is trusted setup code. No fixture instruction executes before the
        # placement and controller readback below. No bwrap --block-fd EOF gate.
        (payload / "cgroup.procs").write_text(str(os.getpid()))
        if own_cgroup() != payload:
            raise ProbeFailure("pre-exec cgroup placement failed")
        for name, expected in (("memory.max", "67108864"), ("pids.max", "32"),
                               ("memory.swap.max", "0"), ("cpu.max", "100000 100000")):
            if (payload / name).read_text().strip() != expected:
                raise ProbeFailure(f"pre-exec limit readback failed: {name}")
        tools = json.loads((work / "tools.json").read_text())
        scratch = work / f"{token}.mount"
        run([tools["mount"], "-t", "tmpfs", "-o",
             "size=1048576,nr_inodes=64,mode=0700,nosuid,nodev,noexec", "tmpfs", str(scratch)])
        emit(evidence, {"event": "placed_before_exec", "pid": os.getpid(), "cgroup": str(payload),
                        "scratch_mount": str(scratch), "bytes": 1048576, "inodes": 64})
        # Duplicate first so incoming descriptors may safely overlap 3 and 4.
        request_copy, reply_copy = os.dup(request), os.dup(reply)
        os.dup2(request_copy, 3, inheritable=True)
        os.dup2(reply_copy, 4, inheritable=True)
        close_except({3, 4})
        filter_fd = os.open(work / "filter.bpf", os.O_RDONLY)
        os.set_inheritable(filter_fd, True)
        command = [tools["bwrap"], "--unshare-user", "--unshare-pid", "--unshare-ipc",
                   "--unshare-net", "--unshare-uts", "--unshare-cgroup", "--new-session",
                   "--cap-drop", "ALL", "--clearenv",
                   "--ro-bind", str(work / "fixture"), "/fixture",
                   "--bind", str(scratch), "/scratch", "--proc", "/proc", "--remount-ro", "/proc",
                   "--dir", "/dev"]
        for device in ("null", "zero", "urandom"):
            command += ["--dev-bind", f"/dev/{device}", f"/dev/{device}",
                        "--remount-ro", f"/dev/{device}"]
        command += ["--symlink", "/scratch", "/tmp", "--symlink", "/scratch", "/dev/shm",
                    "--remount-ro", "/", "--chdir", "/scratch", "--seccomp", str(filter_fd),
                    "--", "/fixture", token]
        # Deliberately omit --die-with-parent in this experiment: only killing
        # MainPID must exercise the surviving systemd owner, not mask a broken
        # service lifetime chain with Bubblewrap's parent-death handling.
        os.execve(tools["bwrap"], command, {})
    except Exception as error:
        try:
            emit(evidence, {"event": "failure", "reason": str(error)})
        except OSError:
            pass
        return 1


def core(work: Path, token: str, request: int, reply: int, lease: int, evidence: int, writer: int) -> int:
    unit = f"penhin-composition-{token}.service"
    try:
        run([str(work / "helper"), "--start", unit, sys.executable, str(SCRIPT), str(work), token,
             str(request), str(reply), str(lease), str(evidence)],
            pass_fds=(request, reply, lease, evidence))
        close_except({writer})
        # Block while keeping only the lifetime write end. The test SIGKILLs this
        # process; EOF must reach the independently systemd-owned supervisor.
        sys.stdin.buffer.read()
        return 0
    except Exception as error:
        emit(evidence, {"event": "failure", "reason": str(error)})
        return 1


class Host:
    def __init__(self, work: Path, systemctl: str):
        self.token = uuid.uuid4().hex
        self.unit = f"penhin-composition-{self.token}.service"
        self.systemctl = systemctl
        self.cgroup: Path | None = None
        self.pidfds: list[int] = []
        self.buffers: dict[int, bytes] = {}
        self.events: list[dict[str, object]] = []
        self.process: subprocess.Popen[bytes] | None = None
        (work / f"{self.token}.mount").mkdir(mode=0o700)
        request_read, self.request = os.pipe()
        self.reply, reply_write = os.pipe()
        lease_read, lease_write = os.pipe()
        self.evidence, evidence_write = os.pipe()
        inherited = (request_read, reply_write, lease_read, evidence_write, lease_write)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-S", str(SCRIPT), "_core", str(work), self.token,
                 *(str(fd) for fd in inherited)],
                pass_fds=inherited, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            for fd in (self.request, self.reply, self.evidence):
                os.close(fd)
            raise
        finally:
            for fd in inherited:
                os.close(fd)

    def message(self, fd: int, timeout: float = 20) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffers.get(fd, b""):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise ProbeFailure(f"{self.unit}: timed out waiting for real fixture evidence")
            data = os.read(fd, 4096)
            if not data:
                raise ProbeFailure(f"{self.unit}: evidence pipe closed before a complete response")
            self.buffers[fd] = self.buffers.get(fd, b"") + data
            if len(self.buffers[fd]) > 65536:
                raise ProbeFailure("fixture evidence exceeded size limit")
        line, self.buffers[fd] = self.buffers[fd].split(b"\n", 1)
        event = json.loads(line)
        self.events.append(event)
        if event.get("event") == "failure":
            raise ProbeFailure(str(event.get("reason")))
        return event

    def ready(self) -> None:
        delegated = self.message(self.evidence)
        if delegated.get("event") != "delegated":
            raise ProbeFailure("missing delegation evidence")
        self.cgroup = Path(str(delegated["cgroup"]))
        placed = self.message(self.evidence)
        if placed.get("event") != "placed_before_exec":
            raise ProbeFailure("missing pre-exec placement evidence")
        fixture = self.message(self.reply)
        if fixture.get("event") != "fixture_ready" or fixture.get("token") != self.token:
            raise ProbeFailure("unexpected sandbox fixture response")
        self.snapshot_processes()

    def challenge(self) -> dict[str, object]:
        os.write(self.request, (self.token + "\n").encode())
        response = self.message(self.reply)
        if response != {"event": "isolated", "token": self.token}:
            raise ProbeFailure("concurrent scratch/channel isolation failed")
        return response

    def snapshot_processes(self) -> None:
        assert self.cgroup is not None
        pids: set[int] = set()
        for path in self.cgroup.rglob("cgroup.procs"):
            pids.update(int(value) for value in path.read_text().split())
        if len(pids) < 4:
            raise ProbeFailure("expected supervisor, Bubblewrap, fixture and surviving descendant")
        for pid in sorted(pids):
            self.pidfds.append(os.pidfd_open(pid))
        self.events.append({"event": "process_snapshot", "pids": sorted(pids)})

    def wait_clean(self, timeout: float = 10) -> None:
        if self.cgroup is None:
            raise ProbeFailure("cannot verify cleanup without the delegated cgroup identity")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            all_exited = all(select.select([fd], [], [], 0)[0] for fd in self.pidfds)
            path = self.cgroup / "cgroup.events"
            try:
                empty = "populated 0" in path.read_text()
            except FileNotFoundError:
                empty = True
            if all_exited and empty:
                self.events.append({"event": "cleanup_verified", "all_pidfds_exited": True,
                                    "cgroup_empty_or_removed": True})
                return
            time.sleep(0.05)
        raise ProbeFailure(f"{self.unit}: processes or cgroup survived cleanup deadline")

    def cleanup(self) -> None:
        errors: list[str] = []
        try:
            if self.process is not None:
                try:
                    if self.process.poll() is None:
                        self.process.kill()
                    self.process.wait(timeout=5)
                except Exception as error:
                    errors.append(str(error))
                finally:
                    if self.process.stdin:
                        self.process.stdin.close()
            # Also stop partially initialized units; a failure must not leave a
            # disposable fixture behind. No global service/configuration changes.
            stopped = subprocess.run([self.systemctl, "--user", "stop", self.unit],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if stopped.returncode:
                shown = subprocess.run([self.systemctl, "--user", "show", self.unit,
                                        "--property=LoadState", "--value"], text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                if shown.stdout.strip() != "not-found":
                    errors.append(f"failed to stop disposable unit {self.unit}: "
                                  + stopped.stderr.decode(errors="replace"))
            if self.cgroup is not None:
                self.wait_clean()
            subprocess.run([self.systemctl, "--user", "reset-failed", self.unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        finally:
            for fd in (self.request, self.reply, self.evidence, *self.pidfds):
                os.close(fd)
        if errors:
            raise ProbeFailure("; ".join(errors))


def experiment(report: dict[str, object]) -> None:
    environment = prerequisites()
    report["environment"] = environment
    tools = environment["tools"]
    assert isinstance(tools, dict)
    hosts: list[Host] = []
    cleanup_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="penhin-composition-") as temporary:
        work = Path(temporary)
        compile_fixture(work, tools)
        try:
            for _ in range(2):
                host = Host(work, tools["systemctl"])
                hosts.append(host)
                host.ready()
            first, second = hosts
            first.challenge()
            second.challenge()
            run([tools["systemctl"], "--user", "kill", "--kill-whom=main",
                 "--signal=SIGKILL", first.unit])
            first.wait_clean()
            second.challenge()  # Other Host still owns its scratch and channel.
            assert second.process is not None
            second.process.kill()
            second.process.wait(timeout=5)
            eof = second.message(second.evidence)
            if eof.get("event") != "lease_eof":
                raise ProbeFailure("core SIGKILL did not reach the supervisor as lease EOF")
            second.wait_clean()
            report["checks"] = {"real_byte_and_inode_exhaustion": True,
                                "extra_broker_pipe_handoff": True, "pre_exec_placement": True,
                                "concurrent_hosts": True, "supervisor_sigkill_cleanup": True,
                                "core_sigkill_cleanup": True}
        finally:
            for host in hosts:
                try:
                    host.cleanup()
                except Exception as error:
                    cleanup_errors.append(str(error))
            report["hosts"] = [{"unit": host.unit, "events": host.events} for host in hosts]
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                raise ProbeFailure("cleanup did not complete: " + "; ".join(cleanup_errors))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    # Internal stages accept only the disposable fixture setup, never payload argv.
    if arguments and arguments[0] == "_supervisor" and len(arguments) == 3:
        return supervisor(Path(arguments[1]), arguments[2])
    if arguments and arguments[0] == "_mount" and len(arguments) == 7:
        return mount_fixture(Path(arguments[1]), arguments[2], Path(arguments[3]),
                             *(int(value) for value in arguments[4:]))
    if arguments and arguments[0] == "_core" and len(arguments) == 8:
        return core(Path(arguments[1]), arguments[2], *(int(value) for value in arguments[3:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the same JSON evidence to this file")
    options = parser.parse_args(arguments)
    report: dict[str, object] = {"schema": 1, "kind": "experimental_composition_probe",
                                "result": "failed", "production_ready": False,
                                "supported_runtime_baseline": False, "remaining_gates": GAPS,
                                "source_revision": "unrecorded",
                                "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                                  for path in (SCRIPT, SOURCE)}, "checks": {}}
    try:
        report["source_revision"] = run(["git", "rev-parse", "HEAD"], cwd=str(SCRIPT.parent))
    except (OSError, ProbeFailure, subprocess.TimeoutExpired):
        pass
    try:
        experiment(report)
        report["result"] = "composition_passed"
    except (Exception, KeyboardInterrupt) as error:
        report["failure"] = f"{type(error).__name__}: {error}"
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if options.output:
        options.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["result"] == "composition_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
