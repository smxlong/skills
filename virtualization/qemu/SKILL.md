---
name: qemu
description: Create, operate, inspect, snapshot, clone, and troubleshoot QEMU virtual machines for experiments, using direct QEMU/QMP or the existing libvirt manager. Use for VM lifecycle, qcow2 disk chains, guest boot/networking, device changes, and reproducible VM labs.
---

# QEMU experiments

First identify the owner: direct `qemu-system-*`, libvirt, or a platform such as
Proxmox. Use that owner's lifecycle/storage API for managed VMs; direct QMP or
offline edits can invalidate its state. QEMU system emulation runs a whole VM;
`qemu-user` runs foreign userspace programs and is a different tool.

Before changing an instance, establish its architecture, acceleration, machine
type, firmware/NVRAM, disks/backing chain, network and control endpoints, and
whether its state is disposable. Record the launch argv or manager definition
and the observation that will demonstrate the experiment worked. Work on a
private overlay of a verified base whenever practical.

## Choose the relevant reference

- [Boot and lifecycle](references/lifecycle.md): host discovery, KVM/TCG,
  firmware, Linux/cloud boot, graceful versus forced shutdown, managed VMs.
- [Storage and checkpoints](references/storage.md): backing graphs, snapshots,
  safe rebase/commit, offline versus live operations, restore and backup.
- [Networking and guest access](references/networking.md): user networking,
  loopback forwarding, isolated multi-VM networks, SSH, consoles and sharing.
- [Control and diagnosis](references/control.md): QMP, guest agent, hotplug,
  block jobs, migration and failure diagnosis.
- [Verified lab](references/lab.md): runnable exercises, actual tested versions,
  demonstrated behavior and untested areas. Use when learning or validating an
  unfamiliar installed version; do not run the whole lab for ordinary tasks.

## Operational invariants

- Discover installed capabilities (`--version`, `-accel help`, `-machine help`,
  `-device help`, QMP `query-commands`/`query-qmp-schema`). Examples are patterns,
  not a claim that every QEMU/version/device supports every operation.
- Keep machine controls private: permission-restricted Unix QMP/QGA sockets;
  loopback-bound forwarded ports/VNC/GDB. These interfaces are not authenticated
  by default. Host networking, PCI passthrough and writable host-directory
  sharing are distinct choices, not prerequisites for running a guest.
- A running VM owns its writable images. Use its manager or QMP for live disk
  changes. Run `qemu-img` mutations only on stopped, exclusively owned images.
  Do not bypass locks with `-U` or repair flags to force an operation through.
- Before rebase/commit/snapshot deletion, draw the backing relationships and
  identify which file changes and whether any sibling depends on it. A disk
  snapshot does not automatically capture RAM, firmware variables or other disks.
- Treat `system_powerdown` as a request, `system_reset` as a hard reset and
  `quit`/kill as abrupt power loss. Observe guest and process completion before
  reporting success or reusing storage. Escalate only the owned process if the
  requested operation permits forced power-off; never use broad `pkill qemu`.
- Preserve image provenance/checksums and keep large runtime artifacts outside
  Git according to the workspace storage policy. A generic skill must not
  embed personal VM names, credentials, host paths or cloud account details.

## Reusable control helper

Run `uv run --no-project python scripts/qmp.py --socket /path/qmp.sock query-status`
from this skill directory. Optional last argument is a JSON object. The helper
negotiates capabilities, matches response IDs, retains interleaved events and
bounds waits. It is for QMP, not QGA, and does not grant ownership of a managed VM.
For asynchronous jobs, a successful command response means accepted: poll the
job/VM status or consume completion events before acting on the result.

Validate an actual operation by its effects: guest readiness rather than PID,
guest-visible disk content rather than image existence, reachable forwarded
service rather than a listening host socket, and restore behavior rather than
snapshot creation alone. Report the exact limits of what was tested.
