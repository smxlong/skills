# Verified experiments and reproduction

For the complete rationale, test matrices, diagrams, failure history and scope
of both laboratories, read [the final report](final-report.md). Use this page
for the shorter VM reproduction procedure.

Validated November 7, 2026 UTC with QEMU 10.0.0, Alpine 3.22 userspace,
Linux 6.12.115-0-virt guest, TCG, q35, one vCPU and 768 MiB guest RAM.
The Docker image ID was
`sha256:882d2bb4afd3f246ebfeafeee18d7cc5a7472224ae53c482bcc8bcec5c3a6c26`
on the follow-up regression run after adding network utilities.
The base Alpine digest was
`sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f`.
The recipe uses distribution repositories: future package versions may change.
Record resolved package/image versions for a new experiment rather than
claiming byte-for-byte reproducibility from a floating package repository.

## Run

From the skill directory, with Docker available:

```bash
docker build -t qemu-lab:local -f scripts/Dockerfile.lab scripts
mkdir -p /absolute/workspace/.private/qemu-lab
docker run --rm --network none \
  -v "$PWD:/skill:ro" \
  -v /absolute/workspace/.private/qemu-lab:/lab \
  qemu-lab:local python3 /skill/scripts/lab.py --work /lab/new-run
uv run --no-project python scripts/test_protocols.py
```

Choose a **new** work directory each time. The lab refuses an existing one;
it retains results, launch argv, serial logs, disks and initramfs for inspection.
Container outputs may be root-owned. The lab needs no privileged container,
host KVM/TAP devices, host networking, published ports or Internet access at
runtime. Package installation requires Internet during image build. The guest's
user-mode network reaches only the enclosing network-disabled container in this
configuration; its host forward is tested from inside that container.

Python helpers use the standard library. The lab's Linux utilities/kernel
are supplied by Dockerfile.lab; qmp.py/qga.py also run with native Python and
Unix sockets. The native-host path requires its own installed QEMU/kernel/tools.
Docker is a convenient lab environment, not a QEMU runtime requirement.

## What passed

The retained [results](verified-results.json) contain 21 checks:

- qcow2 base/overlay/sibling isolation with sector patterns;
- offline internal disk snapshot restoration;
- flattening, guest-content comparison, image growth and integrity check;
- safe rebase to a different base while preserving inherited sectors;
- explicit commit into a disposable backing image;
- real Linux boot, serial console and VirtIO storage;
- loopback TCP forwarding to a guest HTTP responder;
- guest filesystem writes and sync;
- QGA channel synchronization, ping, execution and polled process results;
- guest filesystem freeze, status and thaw in a finally block;
- QMP capabilities/discovery, pause and resume;
- injected link-down/up visible in guest carrier state;
- structured QMP error handling;
- rejection of a concurrent offline resize while the VM owns the disk;
- live internal VM snapshot restoring both RAM and disk content;
- live disk hotplug, guest enumeration, unplug and backend removal;
- atomic external snapshot and block-graph pivot;
- hard reset clearing volatile state while preserving synced disk data;
- quit followed by successful offline image integrity checks;
- external snapshot retaining new writes separately from its checkpoint;
- local Unix-socket migration of a small diskless paused machine, verified
  source postmigrate state and destination resume. This last check does not
  demonstrate shared-storage migration of a running application.

Two additional protocol tests inject interleaved QMP events/errors and stale
QGA bytes before the sync delimiter. They validate framing behavior that a
successful happy-path VM run alone would not establish.

## Lessons from actual failures

- Minimal guest readiness needs a working service test, not only an init marker;
  the first forwarded TCP connection can race service startup.
- Alpine's static BusyBox lacks httpd. The lab uses a tiny nc HTTP responder
  instead of assuming that every BusyBox applet exists.
- Minimal initramfs module autoload needs the correct /sbin/modprobe path and
  compatible module compression. The lab decompresses modules and regenerates
  dependency metadata. Insufficient RAM can yield an incomplete initramfs and
  misleading missing-directory errors; the full-module lab needs more than
  256 MiB. This is not a general minimum for Linux guests.
- devtmpfs/mdev creates the port device but not necessarily the guest-agent
  named symlink. The lab derives that mapping from sysfs explicitly.
- Hotplug on q35's root bus fails; a cold-plugged PCIe root port works.
- Guest block-device removal can precede release of QEMU's block backend.
  Wait for the matching DEVICE_DELETED event before deleting backend nodes;
  this was caught during the follow-up regression and revalidated.
- HMP savevm/loadvm need their text result inspected even inside a successful
  QMP envelope. Actual RAM and disk restoration were checked afterward.

## Limits

The separate [simulated internet lab](simulated-internet.md) validates 24
multi-VM routing, GRE, consumer DHCP/DNS/NAT and CGNAT checks with no actual
network uplink. The VM checks above were all rerun after extracting their
shared initramfs builder. The network lab does not use or validate TAP/bridges.

KVM/HVF/WHPX, ARM, UEFI/NVRAM/TPM, real cloud-image cloud-init/SSH provisioning,
libvirt/Proxmox, TAP/bridges, virtiofs, CPU/memory hotplug, live block
backup/mirror/commit jobs, guest-cooperative ACPI shutdown and cross-host live
migration were documented from primary sources but **not exercised** by this
lab. Test those on a suitable authorized host before reporting proficiency in
that environment. TCG results establish functional behavior, not KVM performance.
