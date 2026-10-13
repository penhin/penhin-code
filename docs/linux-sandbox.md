# Linux Sandbox preparation (#60)

This is preparatory implementation and an executable composition experiment.
There is **no tested native Linux baseline yet**, no supported WSL2 profile,
and no production Sandbox runner wired into Plugin Activation. Existing Plugin
Hosts must not be described as isolated because these tools are present.
Completion of [#60](https://github.com/penhin/penhin-code/issues/60),
[#61](https://github.com/penhin/penhin-code/issues/61), and
[#62](https://github.com/penhin/penhin-code/issues/62) is still required.

## Entry points

From the repository root, inspect prerequisites without starting Plugin code:

```sh
python -m penhin.sandbox
```

The JSON distinguishes `ready_for_composition` from `production_ready`, which
is always false. Installed tools and visible cgroup controllers only make a
machine a candidate for the real experiment. They do not prove delegation,
namespace creation, filtering, quotas, or cleanup. Windows, root execution,
unvalidated WSL profiles, missing tools, an unreachable user manager, and
failed commands produce a nonzero exit with specific checks.

On a native Linux development machine, run the actual experiment:

```sh
python tools/linux_sandbox/composition.py --output composition-report.json
python -m pytest acceptance/linux_sandbox -q
```

The dedicated acceptance entry must fail when enforcement cannot be exercised;
it must not turn missing prerequisites into skips. Ordinary `python -m pytest`
uses `tests/` so Windows development can validate the portable preparation
contracts without pretending to run OS acceptance.

The experiment runs only a fixed, repository-owned C fixture. It has no option
to launch a Plugin or arbitrary command. Its static executable lets the probe
use a minimal filesystem without a broad host-root, home, library, or workspace
mount. That fixture is not the supported Python runtime requested by #60.

## Candidate prerequisites and ownership chain

These are requirements to attempt the probe, not a recorded passing baseline:

- An ordinary user on native Linux with cgroup v2 and a running systemd user
  manager. CPU, memory, and pids controllers must actually be delegated.
- systemd 257 or newer for `StartTransientUnit`'s `ExtraFileDescriptors`.
  The user manager version matters as well as installed command-line tools.
- Bubblewrap, util-linux `unshare` and `mount`, a C compiler with static libc,
  `pkg-config`, `readelf`, and development headers/libraries for libsystemd and
  libseccomp.
- Working unprivileged user/mount/PID/IPC/network namespaces, cgroup kill,
  and tmpfs `size` plus `nr_inodes` limits. Host configuration is never changed
  automatically and no privileged retry is attempted.

The proposed chain is core lease owner → systemd transient user service →
trusted supervisor → delegated payload cgroup → util-linux namespace/mount
setup → Bubblewrap → fixed fixture. The service manager survives supervisor
death; the supervisor observes EOF on a private lease when the core dies.
Only the workload's explicit pipes survive into the fixture. These mechanisms
must be demonstrated together, including during failure, before they determine
the production runner design.

The payload must enter its configured cgroup before `exec`. A trusted launcher
must treat startup-gate EOF as failure: Bubblewrap's `--block-fd` is not a
fail-closed authorization gate. The probe must demonstrate real byte and inode
exhaustion, inventory all writable mounts, pass an explicit broker pipe,
exercise concurrent Hosts, and verify group emptiness after injected deaths.

Bubblewrap's `--size` alone does not set an inode quota. The candidate quota
mount is tmpfs created inside a private mount namespace with both `size` and
`nr_inodes`. Writable aliases must refer to that same bounded scratch or have
independent explicit quotas; non-scratch mounts must be read-only where
applicable. See the [tmpfs documentation](https://docs.kernel.org/filesystems/tmpfs.html),
[systemd delegation](https://systemd.io/CGROUP_DELEGATION/),
[systemd 257 release notes](https://github.com/systemd/systemd/blob/v257/NEWS),
and [Bubblewrap options](https://github.com/containers/bubblewrap/blob/main/bwrap.xml).

## Fixed content preparation

`penhin.sandbox.content.capture_verified_content` takes an ordinary directory,
a separately trusted, complete file-digest manifest, and its pinned identity.
It captures immutable `bytes`; later changes to the source paths do not alter
the captured generation. It rejects symlinks, hardlinks, special files, invalid
relative paths, missing/extra files, digest mismatches, and observed changes
during capture. Linux traversal uses directory descriptors and `O_NOFOLLOW`.
Preparation has explicit size, entry-count, and nesting bounds.

This manifest is an in-memory core input, not a new Plugin Artifact format.
Computing an identity from an untrusted directory does not authenticate it.
Publisher authorization and adapting the existing Artifact signature contract
to these exact captured bytes remain necessary before launch.

On Linux, the content object can retain sealed, non-executable memfds. Sealing
fixes the actual bytes and owns descriptor cleanup; the same retained object
can be reused across Hosts. It is not an executable/runtime profile or a
dependency installer. Windows supports preparation tests only. The memfd step
requires `MFD_NOEXEC_SEAL` support (Linux 6.3+) and verifies seals rather than
depending on host defaults. See [kernel memfd execution controls](https://docs.kernel.org/userspace-api/mfd_noexec.html).

## Evidence still required

Record the distribution, kernel, Bubblewrap, util-linux, Python, libseccomp,
systemd/user-manager versions, controllers, full command, and real probe output
from a native Linux run. Correct failures and repeat the experiment before
calling any combination tested. A passing composition experiment alone is
not #60 acceptance: the explicit Python executable/library/dependency closure,
verified Artifact/signature adaptation, final owned-process runner API,
negative OS tests, immutable environment reuse, and all launch-time failure
paths still need implementation and real contract coverage.

WSL2 remains unsupported until separate Windows interop, inherited channels,
mounted host paths, and enforcement tests pass on a recorded profile. Native
Windows remains #40. Do not close #60 or enable production Plugin isolation
based on the portable unit tests, preflight inventory, or an unexecuted probe.
