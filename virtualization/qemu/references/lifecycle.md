# Boot and lifecycle

## Discover and choose

Inspect OS/architecture (`uname -m`), QEMU executable/version, existing VM
manager, available accelerators and access to `/dev/kvm`. Existence of KVM's
device is not evidence that this user can open it. On Linux, matching guest/
host architectures can use `-accel kvm -cpu host`; TCG supports emulation
without KVM but is slower. macOS uses HVF and Windows may use WHPX; discover
support on the actual executable rather than transferring Linux flags blindly.
In WSL, nested virtualization and device permissions are separate questions.
Don't change host groups/permissions or run privileged containers just to avoid
using TCG for a small experiment. Conversely, don't silently substitute TCG
for a benchmark intended to measure KVM performance.

Use `qemu-system-aarch64` for full ARM guests with its appropriate machine and
firmware. An x86 image does not become an ARM guest by changing `-cpu`.
Capture a versioned machine type for long-lived checkpoints/migrations;
unversioned `q35` is useful for a disposable local experiment.

## Minimal headless pattern

Create a fresh working directory, an overlay (see storage reference), and
permission-restricted control directory. Choose either KVM/host or TCG/max:

```bash
qemu-system-x86_64 \
  -name experiment -machine q35 -accel tcg -cpu max -m 1G -smp 2 \
  -drive file=work.qcow2,format=qcow2,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -display none -serial mon:stdio \
  -qmp unix:qmp.sock,server=on,wait=off
```

`mon:stdio` multiplexes HMP and the serial console: Ctrl-a c switches between
them; Ctrl-a h shows escape help. For automation use `-monitor none -serial
stdio`, capture the output, retain the child PID and use a separate QMP socket.
`-display none` doesn't itself wire a console. `-S` starts CPUs paused; QMP
`cont` starts them. `wait=off` avoids waiting for a management client at boot.

QEMU can start successfully while the guest cannot boot. Check firmware
messages, serial output, boot order, disk format, architecture, RAM, VirtIO
drivers and an OS-appropriate readiness test. A BIOS boot image and a UEFI-only
image need different firmware. For OVMF use read-only CODE and a per-VM copy of
matching writable VARS pflash; don't share writable NVRAM between clones. Save
VARS (and a software TPM's state if used) with a full VM backup/checkpoint.

Direct Linux kernel boot uses `-kernel ... -initrd ... -append 'console=ttyS0 ...'`.
The kernel, modules, root filesystem and console parameters must agree.
The included lab builds a minimal initramfs; a distribution cloud image is more
representative for SSH, services and cloud-init experiments.

## Cloud image initialization

Obtain an architecture-appropriate image and verify the publisher checksum/
signature. Keep it as the immutable base. For cloud-init NoCloud, create
`user-data` beginning `#cloud-config` and `meta-data` with a unique instance-id
and hostname. Provision only an experiment SSH public key; avoid passwords and
host credential directories. User-data is not secret storage.

```bash
xorriso -as mkisofs -output seed.iso -volid cidata -joliet -rock user-data meta-data
```

Attach the seed as a read-only CD drive. Change instance-id for a new instance;
reusing a booted disk can retain cloud-init state. Verify `cloud-init status
--wait` and logs in the guest, then service readiness. Cloud-init keys and
network configuration vary by distro/version. Regenerate SSH host keys and
machine identity for independent clones rather than copying a running identity.

## Lifecycle under a manager

For libvirt inspect the correct URI (`qemu:///session` versus `qemu:///system`)
with `virsh uri`, `list --all`, `dumpxml`, `domblklist --details`, `domiflist` and
`dominfo`. `define` persists XML without starting; `create` starts a transient
domain. `start`, `shutdown`, `reboot`, `suspend`, `resume`, `save`/`restore` and
`managedsave` have different durability/run-state semantics. `destroy` is hard
power-off, not disk deletion. `undefine` removes the definition; storage-removal,
NVRAM and snapshot flags can destroy additional state. Check `virsh help` before
using them. Change live and persistent definitions deliberately; hotplug alone
may disappear at the next boot. On Proxmox use `qm`/its API rather than racing
libvirt or direct QEMU against the platform.

Sources: [QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html),
[managed startup](https://www.qemu.org/docs/master/system/managed-startup.html),
[NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html),
[virsh manual](https://libvirt.org/manpages/virsh.html).
