#!/usr/bin/env python3
"""Isolated six-VM IPv4 internet: routes, GRE, DHCP/DNS, NAT44, CGNAT, faults."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess

from lab import copy_binary, guest_root, pack_initramfs, run, wait_for
from qga import GuestAgent


NODES = {
    'client': [('lan', True, '192.168.50.2/24')],
    'cpe': [('lan', False, '192.168.50.1/24'), ('access', True, '203.0.113.2/30')],
    'carrier': [('access', False, '203.0.113.1/30'), ('subscriber', True, '100.64.2.1/30'), ('backbone', True, '192.0.2.1/30')],
    'core': [('backbone', False, '192.0.2.2/30'), ('service', True, '198.51.100.1/24')],
    'server': [('service', False, '198.51.100.2/24')],
    'subscriber2': [('subscriber', False, '100.64.2.2/30')],
}
ROUTES = {
    'client': ['default via 192.168.50.1'],
    'cpe': ['default via 203.0.113.1'],
    'carrier': ['192.168.50.0/24 via 203.0.113.2', '198.51.100.0/24 via 192.0.2.2'],
    'core': ['default via 192.0.2.1'],
    'server': ['default via 198.51.100.1'],
    'subscriber2': ['default via 100.64.2.1'],
}


def isolated_container():
    if not Path('/.dockerenv').exists() or {name for _, name in socket.if_nameindex()} != {'lo'}:
        raise RuntimeError('Run inside Docker --network none; the enclosing namespace must have only lo')
    return {'interfaces': run('ip', '-j', 'link', 'show'),
            'routes': run('ip', '-j', 'route', 'show'),
            'transport': 'QEMU stream Unix sockets only; management is Unix QGA/QMP',
            'external_uplink': False}


def network_initramfs(work):
    root = guest_root(work)
    for binary in ('ip', 'tc', 'nft', 'conntrack', 'tcpdump', 'socat', 'dnsmasq', 'ping'):
        copy_binary(shutil.which(binary), root)
    (root/'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (root/'etc/group').write_text('root:x:0:\n')
    (root/'etc/lab').mkdir()
    for name, links in NODES.items():
        commands = [f'hostname {name}', 'ip link set lo up',
                    'sysctl -w net.ipv6.conf.all.disable_ipv6=1',
                    f'sysctl -w net.ipv4.ip_forward={int(name in ("cpe", "carrier", "core"))}']
        for index, (_, _, address) in enumerate(links):
            commands += [f'ip link set eth{index} up', f'ip addr add {address} dev eth{index}',
                         f'sysctl -w net.ipv4.conf.eth{index}.rp_filter=0']
        commands += ['sysctl -w net.ipv4.conf.all.rp_filter=0']
        commands += [f'ip route add {route}' for route in ROUTES[name]]
        (root/f'etc/lab/{name}.sh').write_text('\n'.join(commands)+'\n')
    (root/'www/respond').write_text('''#!/bin/sh
while IFS= read -r line; do
  [ "$(printf '%s' "$line" | tr -d '\\r')" = '' ] && break
done
body="node=$(hostname) peer=$SOCAT_PEERADDR:$SOCAT_PEERPORT"
printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\nConnection: close\\r\\n\\r\\n%s' "${#body}" "$body"
''')
    (root/'www/hold').write_text('''#!/bin/sh
printf 'peer=%s:%s\\n' "$SOCAT_PEERADDR" "$SOCAT_PEERPORT"
sleep 30
''')
    for script in ('respond', 'hold'):
        (root/f'www/{script}').chmod(0o755)
    (root/'etc/udhcpc.script').write_text('''#!/bin/sh
case "$1" in
  deconfig) ip addr flush dev "$interface" ;;
  bound|renew)
    ip addr flush dev "$interface"
    ip addr add "$ip/24" dev "$interface"
    ip route replace default via "${router%% *}" dev "$interface"
    : > /etc/resolv.conf
    for server in $dns; do echo "nameserver $server" >> /etc/resolv.conf; done
    ;;
esac
''')
    (root/'etc/udhcpc.script').chmod(0o755)
    (root/'init').write_text('''#!/bin/sh
set -e
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /dev/virtio-ports
mount -t devpts devpts /dev/pts
for module in virtio_pci virtio_net virtio_console nf_tables nf_conntrack nf_nat nft_chain_nat nft_nat nft_masq nft_ct nft_reject_inet ip_gre sch_netem; do
  modprobe "$module" || true
done
mdev -s
node=$(sed -n 's/.*lab.node=\\([^ ]*\\).*/\\1/p' /proc/cmdline)
. "/etc/lab/$node.sh"
for port in /sys/class/virtio-ports/*; do
  [ "$(cat "$port/name")" != org.qemu.guest_agent.0 ] || ln -s "/dev/${port##*/}" /dev/virtio-ports/org.qemu.guest_agent.0
done
socat TCP-LISTEN:80,reuseaddr,fork EXEC:/www/respond >/tmp/http.log 2>&1 &
socat TCP-LISTEN:9000,reuseaddr,fork EXEC:/www/hold >/tmp/hold-server.log 2>&1 &
qemu-ga -m virtio-serial -p /dev/virtio-ports/org.qemu.guest_agent.0 >/tmp/qga.log 2>&1 &
echo INTERNET_LAB_READY
exec /bin/sh
''')
    (root/'init').chmod(0o755)
    return pack_initramfs(root, work/'internet-initramfs.gz')


class Node:
    def __init__(self, name, work, ramdisk, number):
        self.name, self.work, self.agent = name, work, None
        self.output = (work/f'{name}-serial.log').open('wb')
        args = ['qemu-system-x86_64', '-name', name, '-machine', 'q35', '-accel', 'tcg',
                '-cpu', 'max', '-m', '768M', '-smp', '1', '-nodefaults', '-display', 'none',
                '-monitor', 'none', '-serial', 'stdio', '-kernel', '/boot/vmlinuz-virt',
                '-initrd', str(ramdisk), '-append', f'console=ttyS0 quiet panic=-1 lab.node={name}',
                '-qmp', f'unix:{work}/{name}-qmp.sock,server=on,wait=off',
                '-chardev', f'socket,path={work}/{name}-qga.sock,server=on,wait=off,id=qga',
                '-device', 'virtio-serial-pci', '-device', 'virtserialport,chardev=qga,name=org.qemu.guest_agent.0']
        for index, (link, listen, _) in enumerate(NODES[name]):
            path = work/f'{link}.sock'
            if not listen:
                wait_for(path.exists, 15)
            args += ['-netdev', f'stream,id=n{index},server={"on" if listen else "off"},addr.type=unix,addr.path={path}',
                     '-device', f'virtio-net-pci,id=nic{index},netdev=n{index},mac=52:54:00:00:{number:02x}:{index:02x}',
                     '-object', f'filter-dump,id=dump{index},netdev=n{index},file={work}/{name}-eth{index}.pcap']
        (work/f'{name}-argv.json').write_text(json.dumps(args, indent=2)+'\n')
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.output, stderr=self.output)

    def ready(self):
        def booted():
            if self.process.poll() is not None:
                raise RuntimeError(f'{self.name} exited; inspect its serial log')
            return 'INTERNET_LAB_READY' in (self.work/f'{self.name}-serial.log').read_text(errors='replace')
        wait_for(booted, seconds=120)
        self.agent = GuestAgent(self.work/f'{self.name}-qga.sock', timeout=10)
        self.agent.execute('guest-ping')

    def shell(self, command, check=True, timeout=15):
        task = self.agent.execute('guest-exec', {'path':'/bin/sh', 'arg':['-c','set -e\n'+command], 'capture-output':True})
        result = {}
        def completed():
            nonlocal result
            result = self.agent.execute('guest-exec-status', {'pid':task['pid']})
            return result['exited']
        wait_for(completed, seconds=timeout)
        output = base64.b64decode(result.get('out-data','')).decode(errors='replace')
        errors = base64.b64decode(result.get('err-data','')).decode(errors='replace')
        record = {'node':self.name, 'command':command, 'exitcode':result.get('exitcode'), 'stdout':output, 'stderr':errors}
        with (self.work/'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(record)+'\n')
        if result.get('out-truncated') or result.get('err-truncated'):
            raise RuntimeError(f'{self.name}: captured output truncated')
        if check and result.get('exitcode') != 0:
            raise RuntimeError(str(record))
        return record

    def stop(self):
        if self.agent:
            self.agent.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True, help='New output directory inside a network-disabled container')
    args = parser.parse_args()
    isolation = isolated_container()
    work = args.work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    os.chdir(work)
    result = {'qemu':run('qemu-system-x86_64','--version').splitlines()[0],
              'isolation':isolation, 'nodes':NODES, 'checks':[]}
    def passed(name, **details):
        result['checks'].append({'name':name, **details})
        (work/'results.json').write_text(json.dumps(result, indent=2)+'\n')
        print(f'PASS {name}', flush=True)
    passed('enclosing container has only loopback and no external uplink')
    ramdisk = network_initramfs(work)
    nodes = {}
    try:
        for number, name in enumerate(NODES, start=1):
            nodes[name] = Node(name, work, ramdisk, number)
        for node in nodes.values():
            node.ready()
        client,cpe,carrier,core,server,other = [nodes[name] for name in NODES]
        passed('six Linux VMs ready over management sockets; all data links are Unix streams')
        for node in nodes.values():
            node.shell('ip -j addr show; ip -j route show; nft list ruleset')
        def ping(node, address, success=True, options=''):
            response = node.shell(f'ping -n -c 1 -W 2 {options} {address}', check=False)
            assert (response['exitcode']==0) == success, response
            return response
        def http(node, address, success=True):
            response = node.shell(f'wget -q -T 3 -O - http://{address}/', check=False)
            assert (response['exitcode']==0) == success, response
            return response['stdout']
        ping(client,'192.168.50.1')
        passed('single-hop L3 reaches home gateway')
        assert 'peer=192.168.50.2:' in http(client,'198.51.100.2')
        passed('multi-hop routed workload preserves client source IP')
        hops=[]
        for ttl, address in enumerate(('192.168.50.1','203.0.113.1','192.0.2.2'), start=1):
            response=ping(client,'198.51.100.2',success=False,options=f'-t {ttl}')
            assert address in response['stdout'], response
            hops.append(response['stdout'])
        ping(client,'198.51.100.2',options='-t 4')
        passed('TTL expiry exposes all three router hops', trace=hops)
        carrier.shell('sysctl -w net.ipv4.ip_forward=0')
        ping(client,'198.51.100.2',success=False)
        carrier.shell('sysctl -w net.ipv4.ip_forward=1')
        ping(client,'198.51.100.2')
        passed('forwarding disabled breaks path; enabling it restores reachability')
        core.shell('ip route add blackhole 192.168.50.0/24')
        ping(client,'198.51.100.2',success=False)
        core.shell('ip route del blackhole 192.168.50.0/24')
        ping(client,'198.51.100.2')
        passed('return-path blackhole breaks responses despite forward routing; removal restores service')

        # GRE between the access edge and workload across carrier + core.
        client.shell('ip addr add 10.10.0.1/32 dev lo; ip route add 10.20.0.1/32 via 192.168.50.1 src 10.10.0.1')
        cpe.shell('ip tunnel add gre-lab mode gre local 203.0.113.2 remote 198.51.100.2 ttl 64; ip addr add 10.200.0.1/30 dev gre-lab; ip link set gre-lab mtu 1476 up; ip route add 10.20.0.1/32 dev gre-lab; ip route add 10.10.0.1/32 via 192.168.50.2')
        server.shell('ip addr add 10.20.0.1/32 dev lo; ip tunnel add gre-lab mode gre local 198.51.100.2 remote 203.0.113.2 ttl 64; ip addr add 10.200.0.2/30 dev gre-lab; ip link set gre-lab mtu 1476 up; ip route add 10.10.0.1/32 dev gre-lab')
        ping(client,'10.20.0.1')
        assert 'peer=10.10.0.1:' in http(client,'10.20.0.1')
        passed('GRE carries private overlay traffic across routed underlay')
        ping(client,'10.20.0.1',options='-M do -s 1448')
        ping(client,'10.20.0.1',success=False,options='-M do -s 1449')
        passed('GRE MTU permits 1476-byte inner packet and rejects one byte larger with DF')
        carrier.shell("nft add table inet fault; nft 'add chain inet fault forward { type filter hook forward priority -10; policy accept; }'; nft 'add rule inet fault forward ip protocol gre counter drop'")
        ping(client,'198.51.100.2')
        ping(client,'10.20.0.1',success=False)
        carrier.shell('nft delete table inet fault')
        ping(client,'10.20.0.1')
        passed('blocking IP protocol 47 breaks GRE while ordinary underlay IP still works')
        cpe.shell('ip link del gre-lab; ip route del 10.10.0.1/32')
        server.shell('ip link del gre-lab; ip addr del 10.20.0.1/32 dev lo')
        client.shell('ip route del 10.20.0.1/32; ip addr del 10.10.0.1/32 dev lo')

        # Simulated ISP DNS and consumer DHCP, with no external resolvers.
        server.shell('dnsmasq --user=root --group=root --no-resolv --no-hosts --local=/test/ --bind-interfaces --interface=eth0 --host-record=workload.test,198.51.100.2 --pid-file=/run/dns.pid --log-facility=/tmp/dns.log')
        cpe.shell('dnsmasq --user=root --group=root --port=0 --bind-interfaces --interface=eth0 --dhcp-range=192.168.50.100,192.168.50.150,255.255.255.0,1h --dhcp-option=3,192.168.50.1 --dhcp-option=6,198.51.100.2 --dhcp-leasefile=/tmp/leases --pid-file=/run/dhcp.pid --log-facility=/tmp/dhcp.log')
        client.shell('udhcpc -n -q -t 5 -T 2 -i eth0 -s /etc/udhcpc.script')
        lease=client.shell('ip -4 -j addr show dev eth0; ip -4 -j route show default; cat /etc/resolv.conf')['stdout']
        route=json.loads(client.shell('ip -4 -j route show default')['stdout'])
        assert route[0]['gateway']=='192.168.50.1' and '198.51.100.2' in lease, lease
        assert 'node=server' in http(client,'workload.test')
        client_ip=json.loads(client.shell('ip -4 -j addr show dev eth0')['stdout'])[0]['addr_info'][0]['local']
        passed('consumer DHCP supplies address gateway and simulated DNS; hostname resolves locally', lease=lease)
        cpe.shell("nft add table inet edge; nft 'add chain inet edge forward { type filter hook forward priority 0; policy drop; }'; nft 'add rule inet edge forward ct state established,related counter accept'; nft 'add rule inet edge forward iifname eth0 oifname eth1 counter accept'; nft add table ip nat; nft 'add chain ip nat postrouting { type nat hook postrouting priority srcnat; }'; nft 'add rule ip nat postrouting oifname eth1 ip saddr 192.168.50.0/24 counter masquerade'")
        assert 'peer=203.0.113.2:' in http(client,'workload.test')
        passed('consumer NAT44 translates private client to public-facing CPE address')
        http(server,f'{client_ip}:80',success=False)
        passed('consumer stateful firewall blocks unsolicited inbound to LAN')
        cpe.shell(f"nft 'add chain ip nat prerouting {{ type nat hook prerouting priority dstnat; }}'; nft 'add rule ip nat prerouting iifname eth1 tcp dport 8080 counter dnat to {client_ip}:80'; nft 'add rule inet edge forward iifname eth1 ct status dnat tcp dport 80 counter accept'")
        assert 'node=client' in http(server,'203.0.113.2:8080')
        passed('explicit consumer port forward permits only chosen inbound service')

        # Move the customer WAN behind the carrier's shared address space.
        cpe.shell('ip route del default; ip addr del 203.0.113.2/30 dev eth1; ip addr add 100.64.1.2/30 dev eth1; ip route add default via 100.64.1.1; conntrack -F')
        carrier.shell('ip route del 192.168.50.0/24; ip addr del 203.0.113.1/30 dev eth0; ip addr add 100.64.1.1/30 dev eth0; ip addr add 203.0.113.9/32 dev lo; conntrack -F')
        core.shell('ip route add 203.0.113.9/32 via 192.0.2.1')
        carrier.shell("nft add table inet cgn; nft 'add chain inet cgn forward { type filter hook forward priority 0; policy drop; }'; nft 'add rule inet cgn forward ct state established,related counter accept'; nft 'add rule inet cgn forward iifname { eth0, eth1 } oifname eth2 counter accept'; nft add table ip cgnnat; nft 'add chain ip cgnnat postrouting { type nat hook postrouting priority srcnat; }'; nft 'add rule ip cgnnat postrouting oifname eth2 ip saddr 100.64.0.0/10 meta l4proto { tcp, udp } counter snat to 203.0.113.9:40000-40009'; nft 'add rule ip cgnnat postrouting oifname eth2 ip saddr 100.64.0.0/10 counter snat to 203.0.113.9'")
        assert 'peer=203.0.113.9:' in http(client,'workload.test')
        assert 'peer=203.0.113.9:' in http(other,'198.51.100.2')
        passed('double NAT and second subscriber share one simulated public carrier address')
        assert 'src='+client_ip in cpe.shell('conntrack -L -p tcp')['stdout']
        mappings=carrier.shell('conntrack -L -p tcp')['stdout']
        assert 'src=100.64.1.2' in mappings and 'src=100.64.2.2' in mappings, mappings
        passed('conntrack shows private-to-shared and shared-to-public translation layers', carrier_mappings=mappings)
        for node in (client,other):
            node.shell('socat -u TCP:198.51.100.2:9000,sourceport=40000 CREATE:/tmp/held-peer >/tmp/held-out 2>/tmp/held-error </dev/null &')
        def peers_ready():
            return all('peer=' in node.shell('cat /tmp/held-peer 2>/dev/null || true')['stdout'] for node in (client,other))
        wait_for(peers_ready,seconds=15)
        peers=[node.shell('cat /tmp/held-peer')['stdout'].strip() for node in (client,other)]
        assert all('peer=203.0.113.9:' in peer for peer in peers) and len(set(peers))==2, peers
        passed('CGNAT PAT remaps colliding subscriber source ports for same destination', observed_peers=peers)
        http(server,'203.0.113.9:8080',success=False)
        http(server,'100.64.1.2:8080',success=False)
        passed('customer port forward alone cannot cross carrier NAT or its inbound firewall')
        carrier.shell("nft 'add chain ip cgnnat prerouting { type nat hook prerouting priority dstnat; }'; nft 'add rule ip cgnnat prerouting iifname eth2 ip daddr 203.0.113.9 tcp dport 18080 counter dnat to 100.64.1.2:8080'; nft 'add rule inet cgn forward iifname eth2 ct status dnat tcp dport 8080 counter accept'")
        assert 'node=client' in http(server,'203.0.113.9:18080')
        passed('explicit carrier plus customer DNAT publishes selected inner service')
        http(server,'203.0.113.9:18081',success=False)
        passed('adjacent unmapped public port stays inaccessible')

        carrier.shell('tc qdisc add dev eth2 root netem delay 100ms')
        latency=ping(client,'198.51.100.2')['stdout']
        rtt=re.search(r'time[=<]([\d.]+)',latency)
        assert rtt and float(rtt.group(1)) >= 90, latency
        passed('netem adds measured delay on selected simulated egress', ping=latency)
        carrier.shell('tc qdisc replace dev eth2 root netem loss 100%')
        ping(client,'198.51.100.2',success=False)
        carrier.shell('tc qdisc del dev eth2 root')
        ping(client,'198.51.100.2')
        passed('deterministic packet-loss injection breaks service and cleanup restores it')
        for node in nodes.values():
            node.shell('ip -j addr show; ip -j route show; nft list ruleset; conntrack -L 2>/dev/null || true')
    finally:
        for node in reversed(list(nodes.values())):
            try:
                if node.agent:
                    node.shell('cat /tmp/dhcp.log /tmp/dns.log /tmp/http.log /tmp/qga.log 2>/dev/null || true', check=False)
            except Exception as error:
                print(f'Diagnostic collection failed for {node.name}: {error}', flush=True)
            finally:
                try:
                    node.stop()
                except Exception as error:
                    print(f'Cleanup failed for {node.name}: {error}', flush=True)
    gre=run('tcpdump','-nn','-r',work/'carrier-eth2.pcap','ip proto 47')
    assert 'GRE' in gre,gre
    (work/'gre-capture.txt').write_text(gre)
    passed('carrier-link packet capture independently confirms GRE encapsulation', capture_excerpt=gre.splitlines()[:6])
    assert all(node.process.poll() is not None for node in nodes.values())
    assert json.loads(isolated_container()['routes']) == []
    passed('all guests stopped; enclosing network remains loopback-only with no routes')
    print(f'{len(result["checks"])} checks passed; artifacts in {work}',flush=True)


if __name__ == '__main__':
    main()
