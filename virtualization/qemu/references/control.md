# Control, guest operations and diagnosis

## QMP versus guest agent

QMP controls the emulated machine. The guest agent (QGA) is a guest service
with guest-level powers; one working does not imply the other works.

QMP: read the greeting, send `qmp_capabilities`, then send JSON requests with
IDs. Match responses by ID while processing asynchronous events. Don't assume
the next JSON line is the response. QMP sockets may appear before guest boot.
Use the helper in `scripts/qmp.py` for one-shot requests, or import `QMP` to
maintain a connection and retain events across a sequence. A timeout makes a
mutation's outcome uncertain; query actual state before retrying it.

Useful discovery/observation: `query-status`, `query-version`, `query-commands`,
`query-qmp-schema`, `query-block`, `query-named-block-nodes`, `query-blockstats`,
`query-cpus-fast`, `query-pci`, `query-memory-devices`. Use explicit node/device
IDs when constructing a graph; auto-generated `#block...` names are discovered,
not stable identifiers to hardcode.

`stop` pauses CPUs, `cont` resumes, `system_powerdown` requests ACPI shutdown,
`system_reset` hard-resets, `quit` terminates QEMU. Pausing doesn't flush an
application's buffers. Observe run state, guest readiness or process exit as
appropriate. If using `human-monitor-command` for an HMP-only operation such as
`savevm`, inspect the returned text: a QMP success wrapper can contain an HMP
error. HMP output isn't a stable structured automation API.

## QGA

Connect a private host socket to a `virtio-serial` port named exactly
`org.qemu.guest_agent.0`; install/start qemu-ga inside the guest with the matching
device path. The agent may need a writable state directory and appropriate
permissions. For direct QEMU:

In a minimal initramfs, devtmpfs may create `/dev/vport*` without the usual
`/dev/virtio-ports/` symlink rules. Inspect `/sys/class/virtio-ports/*/name` and
create the matching named link or configure the agent for the actual port;
an emulated port being present does not prove its named guest path exists.

```bash
-chardev socket,path=qga.sock,server=on,wait=off,id=qga0 \
-device virtio-serial-pci \
-device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0
```

QGA has no QMP greeting or `qmp_capabilities`. A reused channel can contain old
partial traffic. `scripts/qga.py` performs `guest-sync-delimited` synchronization
with a unique token/sentinel before sending an RPC. Use it with the same CLI
shape as qmp.py. Timeouts can leave unknown command outcomes; re-synchronize
and inspect rather than blindly repeating a guest mutation.

`guest-info` reports supported/enabled commands, `guest-ping` tests responsiveness,
and network/filesystem queries report guest-visible state. `guest-exec` returns
a process ID, not the completed command result; poll `guest-exec-status`, check
exit code/signal/truncation flags and decode base64 output. Agent RPCs may be
disabled by distribution policy. The agent is powerful remote execution, not a
reason to weaken SSH or expose a host control socket over the network.

For filesystem freeze use a bounded operation, `try/finally` thaw and verify
`guest-fsfreeze-status` afterward. Freeze may intentionally block many other
agent commands. Don't execute a guest command that needs the frozen filesystem
while waiting for that same command to trigger thaw. Filesystem consistency is
not database/application consistency.

## Device and network experiments

For disk hotplug add the file node with `blockdev-add`, then its format node,
then `device_add` referencing the format node. On q35, provision a suitable
`pcie-root-port` at launch and attach the hotplug device to that port; `pcie.0`
itself does not provide hotplug slots. Check guest enumeration, not just QMP.
Before unplugging, stop guest users/unmount; request `device_del`, then wait
for `DEVICE_DELETED` (the helper's `wait_event` supports this) or confirmed host
device removal before `blockdev-del`. Guest enumeration can disappear before
QEMU finishes releasing its backend. Remove
format before file backend. Rejected devices don't imply their backend nodes
were automatically cleaned up. Device capabilities/slots constrain memory and
CPU hotplug too; don't extrapolate from a successful disk addition.

`set_link` can inject a link failure without replacing the guest. Test carrier
or service behavior inside the guest and restore the link. Changing link state
is different from a simulated partition, packet loss, latency or DNS failure;
those require an explicitly scoped network setup.

## Migration and debugging

For a local migration experiment create a matching destination machine with
`-incoming defer`, issue `migrate-incoming` on a private Unix socket URI, then
`migrate` from the source. Poll `query-migrate` and check source/destination
states, failure details and destination readiness. `completed` is not a promise
that a destination guest application is healthy. Arrange identical machine,
CPU, firmware, device and storage topology; migration usually doesn't copy
disk content. Coordinate storage ownership to prevent two writers. Live remote
migration also needs authorization, secure transport, compatibility and a
failure plan; do not publish an unauthenticated migration listener.

GDB (`-S` plus a loopback/Unix `-gdb` endpoint), guest-memory dumps, tracing
(`-trace help`) and `-d help` are targeted diagnosis tools. Dumps may contain
credentials and personal memory; keep them private. Start by preserving argv,
stderr, serial output, QMP state and block errors. Use one controlled change
and a bounded wait rather than repeatedly restarting a guest into damaged
storage. A stopped child process, socket file or snapshot file alone is not
evidence that an operation succeeded.

Sources: [QMP protocol](https://www.qemu.org/docs/master/interop/qmp-spec.html),
[QMP commands](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html),
[QGA service](https://www.qemu.org/docs/master/interop/qemu-ga.html),
[QGA commands](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html).
