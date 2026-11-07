#!/usr/bin/env python3
"""Reproducible throwaway QEMU experiments; no host disks or privileged network."""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import urllib.request

from qmp import QMP
from qga import GuestAgent


def run(*args):
    return subprocess.check_output([str(arg) for arg in args], stderr=subprocess.STDOUT).decode()


def wait_for(test, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.1)
    raise TimeoutError('Experiment did not reach expected state')


def copy_binary(binary, root):
    binary = Path(binary)
    target = root / binary.relative_to('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, target)
    result = subprocess.run(['ldd', str(binary)], text=True, capture_output=True)
    for library in set(re.findall(r'(/[\w./+-]+)', result.stdout + result.stderr)):
        if Path(library).is_file():
            dest = root / library.lstrip('/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(library, dest)


def initramfs(work):
    root = work / 'root'
    for directory in ('bin', 'proc', 'sys', 'dev', 'tmp', 'run', 'var/run', 'mnt/disk', 'www', 'etc'):
        (root / directory).mkdir(parents=True)
    shutil.copy2('/bin/busybox.static', root / 'bin/busybox')
    (root / 'bin/sh').symlink_to('busybox')
    copy_binary(shutil.which('qemu-ga'), root)
    shutil.copytree('/lib/modules', root / 'lib/modules', symlinks=True)
    # Minimal BusyBox's module loader must agree with kernel autoload paths.
    (root / 'sbin').symlink_to('bin')
    for compressed in (root / 'lib/modules').rglob('*.ko.gz'):
        compressed.with_suffix('').write_bytes(gzip.decompress(compressed.read_bytes()))
        compressed.unlink()
    version = next((root / 'lib/modules').iterdir()).name
    run('depmod', '-b', root, version)
    (root / 'www/index.html').write_text('qemu-lab-network-ok\n')
    (root / 'www/respond').write_text('''#!/bin/sh
while IFS= read -r line; do
  [ "$(printf '%s' "$line" | tr -d '\\r')" = '' ] && break
done
printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 20\\r\\nConnection: close\\r\\n\\r\\nqemu-lab-network-ok\\n'
''')
    (root / 'www/respond').chmod(0o755)
    (root / 'init').write_text('''#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
for module in virtio_pci virtio_blk virtio_net virtio_console crc32c_generic ext4; do modprobe "$module"; done
for attempt in 1 2 3 4 5; do
  mdev -s
  [ -b /dev/vda ] && break
  sleep 1
done
ip link set lo up
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ip route add default via 10.0.2.2
mount /dev/vda /mnt/disk || { ls -l /dev/vd* /sys/block; cat /proc/modules; dmesg | tail -30; exec /bin/sh; }
while true; do nc -l -p 80 -e /www/respond; done &
mkdir -p /dev/virtio-ports
for port in /sys/class/virtio-ports/*; do
  [ "$(cat "$port/name")" = org.qemu.guest_agent.0 ] && ln -s "/dev/${port##*/}" /dev/virtio-ports/org.qemu.guest_agent.0
done
qemu-ga -m virtio-serial -p /dev/virtio-ports/org.qemu.guest_agent.0 &
echo QEMU_LAB_READY
export PS1='lab# '
exec /bin/sh
''')
    (root / 'init').chmod(0o755)
    names = ['.'] + sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    packed = subprocess.check_output(['cpio', '-o', '-H', 'newc'], cwd=root,
                                     input=('\n'.join(names) + '\n').encode(), stderr=subprocess.DEVNULL)
    target = work / 'initramfs.gz'
    target.write_bytes(gzip.compress(packed, compresslevel=1))
    return target


def migrate_machine(work):
    processes = []
    logs = []
    common = ['qemu-system-x86_64', '-machine', 'q35', '-accel', 'tcg',
              '-cpu', 'max', '-m', '64M', '-nodefaults', '-display', 'none', '-S', '-nic', 'none']
    try:
        for name in ('destination', 'source'):
            log = (work/f'{name}.log').open('wb')
            logs.append(log)
            args = common + ['-qmp', f'unix:{work/name}.sock,server=on,wait=off']
            if name == 'destination':
                args += ['-incoming', 'defer']
            processes.append(subprocess.Popen(args, stdout=log, stderr=log))
        wait_for(lambda: all((work/f'{name}.sock').exists() for name in ('source', 'destination')), 15)
        with QMP(work/'destination.sock') as dest, QMP(work/'source.sock') as source:
            uri = f'unix:{work}/migration.sock'
            dest.execute('migrate-incoming', {'uri':uri})
            source.execute('migrate', {'uri':uri})
            wait_for(lambda: source.execute('query-migrate')['status'] == 'completed', 30)
            assert source.execute('query-status')['status'] == 'postmigrate'
            dest.execute('cont')
            assert dest.execute('query-status')['running']
            dest.execute('quit')
            source.execute('quit')
        for process in processes:
            process.wait(timeout=10)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True,
                        help='New empty directory; artifacts are retained for inspection')
    args = parser.parse_args()
    work = args.work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    os.chdir(work)
    evidence = {'qemu': run('qemu-system-x86_64', '--version').splitlines()[0],
                'accelerator': 'tcg', 'checks': []}

    def passed(name, detail=None):
        evidence['checks'].append({'name': name, 'detail': detail})
        (work / 'results.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(f'PASS {name}', flush=True)

    # Offline images: independent branches, internal disk snapshots and flattening.
    run('qemu-img', 'create', '-f', 'qcow2', 'base.qcow2', '64M')
    run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x11 0 4k', 'base.qcow2')
    for name in ('branch', 'sibling'):
        run('qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(work/'base.qcow2'), f'{name}.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x22 0 4k', 'branch.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x11 0 4k', 'sibling.qcow2')
    passed('overlay isolation', json.loads(run('qemu-img', 'info', '--backing-chain', '--output=json', 'branch.qcow2')))
    run('qemu-img', 'snapshot', '-c', 'before', 'branch.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x33 0 4k', 'branch.qcow2')
    run('qemu-img', 'snapshot', '-a', 'before', 'branch.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x22 0 4k', 'branch.qcow2')
    passed('offline internal disk snapshot restore')
    run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', 'branch.qcow2', 'flat.qcow2')
    run('qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2', 'branch.qcow2', 'flat.qcow2')
    assert 'backing-filename' not in json.loads(run('qemu-img', 'info', '--output=json', 'flat.qcow2'))
    run('qemu-img', 'resize', 'flat.qcow2', '+16M')
    assert json.loads(run('qemu-img', 'info', '--output=json', 'flat.qcow2'))['virtual-size'] == 80*1024*1024
    run('qemu-img', 'check', '-f', 'qcow2', 'flat.qcow2')
    passed('flatten compare grow and integrity check')

    # Safe rebase preserves guest content, commit intentionally changes a private base.
    run('qemu-img', 'create', '-f', 'qcow2', 'different.qcow2', '64M')
    run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x44 4k 4k', 'different.qcow2')
    run('qemu-img', 'rebase', '-f', 'qcow2', '-F', 'qcow2', '-b', str(work/'different.qcow2'), 'branch.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x22 0 4k', '-c', 'read -P 0x00 4k 4k', 'branch.qcow2')
    passed('safe rebase preserves inherited sectors')
    run('qemu-img', 'commit', '-f', 'qcow2', 'branch.qcow2')
    run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x22 0 4k', 'different.qcow2')
    passed('commit writes to backing image in isolated chain')

    ramdisk = initramfs(work)
    run('truncate', '-s', '128M', 'guest.raw')
    run('mkfs.ext4', '-F', 'guest.raw')
    run('qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2', 'guest.raw', 'guest.qcow2')
    # Unix sockets are intentionally short, independent of bind-mount path length.
    qmp_path, qga_path = work/'qmp.sock', work/'qga.sock'
    kernel = '/boot/vmlinuz-virt'
    command = ['qemu-system-x86_64', '-machine', 'q35', '-accel', 'tcg', '-cpu', 'max',
        '-m', '768M', '-smp', '1', '-display', 'none', '-monitor', 'none',
        '-kernel', kernel, '-initrd', str(ramdisk), '-append', 'console=ttyS0 quiet panic=-1',
        '-drive', 'file=guest.qcow2,format=qcow2,if=virtio',
        '-serial', 'stdio', '-qmp', f'unix:{qmp_path},server=on,wait=off',
        '-netdev', 'user,id=n0,hostfwd=tcp:127.0.0.1:18080-:80', '-device', 'virtio-net-pci,id=nic0,netdev=n0',
        '-device', 'pcie-root-port,id=rp1,chassis=1,slot=1',
        '-chardev', f'socket,path={qga_path},server=on,wait=off,id=qga0',
        '-device', 'virtio-serial-pci', '-device', 'virtserialport,chardev=qga0,name=org.qemu.guest_agent.0']
    (work/'command.json').write_text(json.dumps(command, indent=2)+'\n')
    output = (work/'serial.log').open('wb')
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=output)
    try:
        wait_for(lambda: 'QEMU_LAB_READY' in (work/'serial.log').read_text(errors='replace'))
        assert process.poll() is None
        passed('Linux guest boots under TCG with serial console and virtio disk')

        def guest(shell):
            token = 'DONE_' + str(time.monotonic_ns())
            start = (work/'serial.log').stat().st_size
            process.stdin.write((f'sh -c {shlex.quote("set -e; " + shell)}; '
                + f"rc=$?; printf '\\n{token}:%s\\n' \"$rc\"\n").encode())
            process.stdin.flush()
            wait_for(lambda: re.search(r'\r?\n'+token+r':\d+\r?\n',
                (work/'serial.log').read_bytes()[start:].decode(errors='replace')) is not None)
            text = (work/'serial.log').read_bytes()[start:].decode(errors='replace')
            status = int(re.search(r'\r?\n'+token+r':(\d+)\r?\n', text).group(1))
            assert status == 0, text
            return text

        def http_ready():
            try:
                with urllib.request.urlopen('http://127.0.0.1:18080', timeout=2) as response:
                    return response.read() == b'qemu-lab-network-ok\n'
            except OSError:
                return False
        wait_for(http_ready, seconds=20)
        passed('user network host forward reaches guest HTTP server')
        assert 'persisted' in guest('echo persisted > /mnt/disk/proof; sync; cat /mnt/disk/proof')
        passed('guest filesystem read write and sync')
        with GuestAgent(qga_path) as agent:
            agent.execute('guest-ping')
            task = agent.execute('guest-exec', {'path':'/bin/sh', 'arg':['-c', 'cat /mnt/disk/proof'], 'capture-output': True})
            result = {}
            def completed():
                nonlocal result
                result = agent.execute('guest-exec-status', {'pid':task['pid']})
                return result['exited']
            wait_for(completed, seconds=15)
            assert result['exitcode'] == 0 and base64.b64decode(result['out-data']) == b'persisted\n'
            passed('guest agent synchronization ping exec and exit-status polling')
            try:
                assert agent.execute('guest-fsfreeze-freeze-list', {'mountpoints':['/mnt/disk']}) == 1
                assert agent.execute('guest-fsfreeze-status') == 'frozen'
            finally:
                agent.execute('guest-fsfreeze-thaw')
            passed('guest agent filesystem freeze and guaranteed thaw')
        with QMP(qmp_path) as qmp:
            assert qmp.execute('query-status')['running']
            commands = {item['name'] for item in qmp.execute('query-commands')}
            qmp.execute('stop')
            assert qmp.execute('query-status')['status'] == 'paused'
            qmp.execute('cont')
            passed('QMP negotiation discovery pause resume', {'commands': len(commands)})
            qmp.execute('set_link', {'name':'nic0', 'up':False})
            assert re.search(r'\n0\r?\n', guest('cat /sys/class/net/eth0/carrier'))
            qmp.execute('set_link', {'name':'nic0', 'up':True})
            assert re.search(r'\n1\r?\n', guest('cat /sys/class/net/eth0/carrier'))
            passed('runtime link down and up reflected inside guest')
            try:
                qmp.execute('not-a-real-command')
            except RuntimeError as error:
                assert 'CommandNotFound' in str(error)
            else:
                raise AssertionError('QMP errors were not recognized')
            passed('QMP structured error handling')
            # Offline writes must fail while QEMU holds this disk.
            locked = subprocess.run(['qemu-img', 'resize', 'guest.qcow2', '+1M'], capture_output=True)
            assert locked.returncode != 0 and b'lock' in locked.stderr.lower()
            passed('active image rejects concurrent offline mutation')
            guest('echo before > /tmp/memory; echo before > /mnt/disk/proof; sync')
            result = qmp.execute('human-monitor-command', {'command-line': 'savevm checkpoint'})
            assert not result.strip(), result
            guest('echo after > /tmp/memory; echo after > /mnt/disk/proof; sync')
            result = qmp.execute('human-monitor-command', {'command-line': 'loadvm checkpoint'})
            assert not result.strip(), result
            restored = guest('cat /tmp/memory /mnt/disk/proof')
            assert restored.count('before') >= 2, restored
            passed('live internal VM snapshot restores both RAM and guest disk')
            run('qemu-img', 'create', '-f', 'qcow2', 'hotplug.qcow2', '16M')
            qmp.execute('blockdev-add', {'driver':'file', 'node-name':'hotfile', 'filename':str(work/'hotplug.qcow2')})
            qmp.execute('blockdev-add', {'driver':'qcow2', 'node-name':'hotdisk', 'file':'hotfile'})
            qmp.execute('device_add', {'driver':'virtio-blk-pci', 'drive':'hotdisk', 'id':'hotdev', 'bus':'rp1'})
            assert 'vdb' in guest('sleep 1; ls /sys/block')
            qmp.execute('device_del', {'id':'hotdev'})
            wait_for(lambda: 'vdb' not in guest('ls /sys/block'))
            qmp.execute('blockdev-del', {'node-name':'hotdisk'})
            qmp.execute('blockdev-del', {'node-name':'hotfile'})
            passed('live disk hotplug guest detection unplug and backend removal')
            source = next(block['inserted']['node-name'] for block in qmp.execute('query-block') if block.get('device') == 'virtio0')
            qmp.execute('transaction', {'actions':[{'type':'blockdev-snapshot-sync', 'data':{
                'node-name':source, 'snapshot-file':str(work/'external.qcow2'),
                'snapshot-node-name':'external-layer', 'format':'qcow2'}}]})
            guest('echo external-layer > /mnt/disk/proof; sync')
            assert any(block.get('inserted',{}).get('node-name') == 'external-layer' for block in qmp.execute('query-block'))
            passed('atomic live external disk snapshot pivots active block graph')
            boots = (work/'serial.log').read_text(errors='replace').count('QEMU_LAB_READY')
            qmp.execute('system_reset')
            wait_for(lambda: (work/'serial.log').read_text(errors='replace').count('QEMU_LAB_READY') > boots)
            assert 'external-layer' in guest('test ! -f /tmp/memory; cat /mnt/disk/proof')
            wait_for(http_ready, seconds=20)
            passed('hard reset reboots guest with persistent disk and cleared volatile state')
            qmp.execute('quit')
        process.wait(timeout=15)
        run('qemu-img', 'check', '-f', 'qcow2', 'guest.qcow2')
        passed('QMP quit releases disk for offline integrity check')
        run('qemu-img', 'check', '-f', 'qcow2', 'external.qcow2')
        assert subprocess.run(['qemu-img', 'compare', '-f','qcow2','-F','qcow2',
            'guest.qcow2','external.qcow2'], capture_output=True).returncode == 1
        passed('external snapshot keeps new writes separate from checkpoint')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        output.close()
    migrate_machine(work)
    passed('local Unix-socket migration of diskless paused machine and destination resume')
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == '__main__':
    main()
