# Storage, checkpoints and recovery

## Establish the graph

With all affected writers stopped, use explicit image formats and inspect:

```bash
qemu-img info --backing-chain --output=json work.qcow2
qemu-img check -f qcow2 work.qcow2
qemu-img create -f qcow2 -F qcow2 -b /absolute/base.qcow2 work.qcow2
```

Only the overlay receives guest writes. Relative backing paths resolve relative
to the image, not your shell's working directory. Moving an overlay alone can
break its chain. Inspect every ancestor and preserve dependencies. An immutable
base can be shared; committing into it changes what every sibling sees.

qcow2 virtual capacity, allocated file size and host free space are different.
Sparse files can become large when copied without sparse support. Check `du`,
`ls -l`, virtual-size and host filesystem headroom. Specify `-f`/`-F` instead of
probing untrusted formats. Arbitrary disk images can reference external files;
inspect untrusted artifacts in an isolated environment before opening them.

## Choose the right checkpoint

| Mechanism | Captures | Important limit |
| --- | --- | --- |
| Separate qcow2 overlay | Subsequent disk writes | Depends on unchanged ancestors |
| `qemu-img snapshot -c/-a/-d NAME` | Internal image disk state | Offline; not a running VM's RAM |
| HMP `savevm` / `loadvm` | Supported VM RAM/device state and writable disks | Device/backend support and matching VM config required |
| Live external snapshot | Pivot of active block graph to overlay | Disk checkpoint, not RAM; quiesce for application consistency |
| QEMU migration/save stream | RAM/device state | Normally excludes disk bytes; storage coordinated separately |

Snapshots are not independent backups. Boot a restored copy and verify actual
guest content. For multi-disk guests use an atomic manager/QMP transaction and
application coordination, not a sequential set of unrelated disk checkpoints.

## Offline transformations

```bash
qemu-img convert -f qcow2 -O qcow2 work.qcow2 standalone.qcow2
qemu-img compare -f qcow2 -F qcow2 work.qcow2 standalone.qcow2
qemu-img resize -f qcow2 standalone.qcow2 +8G
```

Conversion without a new backing image flattens the guest-visible disk. It does
not preserve the source's full snapshot history or running VM state. Compare
before reuse; do not use strict allocation comparison when only byte content
equivalence is intended. Growing capacity does not resize guest partitions,
LVM/encryption or filesystems. Grow those in their proper order. Shrinking
requires a separately planned filesystem/partition shrink and backup.

`qemu-img rebase -f qcow2 -F qcow2 -b NEWBASE work.qcow2` performs a safe rebase:
it reads old/new ancestors and materializes differences to preserve guest bytes.
`rebase -u` only edits metadata; use solely when proving the backing content is
equivalent (for example relocating the same base). Wrong metadata-only rebases
silently change guest data. `qemu-img commit` writes overlay data into its backing
file; draw the graph and isolate siblings before using it. Do not assume the
overlay remains a complete independent recovery point afterward.

Use `qemu-img check` without repair first. `check -r` can change evidence and
cannot repair guest filesystems; preserve the source and understand the failure.
Never mutate or convert an actively written image with offline tools. A paused
VM still owns its images, and forced shared opens do not create consistency.

## Live operations

Use the VM owner. QMP can add nodes and atomically pivot using `transaction`
with `blockdev-snapshot-sync` or an appropriate blockdev snapshot operation.
Query `query-block`/`query-named-block-nodes` to identify exact node names.
`blockdev-backup`, `block-stream`, `block-commit` and `blockdev-mirror` are
different graph operations; discover the installed command schema. Jobs have
asynchronous completion/error states. A mirror becoming ready is not the same
as completing/pivoting it; a commit may invalidate sibling dependants. Don't
delete files merely because a command was accepted.

For libvirt use its snapshot/blockjob/backup commands so definitions and
security labels stay synchronized. `--quiesce` requires a functioning guest
agent; filesystem freeze is not equivalent to a database-consistent backup.
If freezing, establish a bounded operation and guaranteed thaw path. For
application consistency coordinate database checkpoints/hooks or shut down.

Preserve backing images or flatten, configuration, firmware variables, disk
checksums and any encryption/TPM prerequisites. Store large artifacts according
to the enclosing workspace's policy; never put live VM images in source control.

Sources: [qemu-img](https://www.qemu.org/docs/master/tools/qemu-img.html),
[VM/image snapshots](https://www.qemu.org/docs/master/system/images.html),
[QMP block commands](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html),
[libvirt backing chains](https://libvirt.org/kbase/backing_chains.html).
