# Networking and access

For an entirely artificial internet with routers, workload nodes, GRE and
consumer/carrier NAT, use [simulated-internet.md](simulated-internet.md).
That workflow deliberately has **no** user-mode uplink or host forwarding.

Start with unprivileged user-mode networking for an ordinary disposable VM:

```bash
-netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
-device virtio-net-pci,id=nic0,netdev=net0
```

Default user networking gives the guest DHCP/DNS and outbound access; it does
not isolate the guest from all host services. Typically the guest address starts
at 10.0.2.15, the host-facing gateway is 10.0.2.2 and DNS is 10.0.2.3. A listening
host forward does not prove a service is ready inside the guest. Check guest
address, routes, firewall, service bind address and TCP response. Ping outside
the virtual network can be limited by host ICMP permissions; use a TCP test
instead of concluding all networking is broken.

`-nic none` is explicit no-network. `-netdev user,...,restrict=on` restricts guest
network access but deliberate forwarding still matters; test the exact policy.
TAP/bridge gives different reachability and usually requires host configuration.
Do not change the host bridge/default route/firewall for a NAT experiment.
For reproducible multi-VM networks, consider socket/stream networking or a
libvirt isolated network, assigning unique MAC/IP addresses and checking that
there is no unintended uplink. Backend names/flags vary: use installed help.

## SSH and console

```bash
ssh -p 2222 -i experiment_key \
  -o UserKnownHostsFile=./known_hosts -o StrictHostKeyChecking=accept-new \
  experiment@127.0.0.1
```

Use a per-lab known_hosts file. A rebuilt guest may legitimately change keys,
but verify its identity before removing the old entry; don't globally disable
host-key checks. No SSH forwarding is useful until sshd, a user, the public key
and guest networking are configured. A serial console can diagnose failures
before network readiness. In libvirt, `virsh domifaddr` can depend on DHCP lease
visibility or a guest agent and may be empty despite a working guest.

QMP, HMP, QGA, serial console, VNC and SSH are distinct channels. VNC/GDB/control
sockets can grant powerful unauthenticated access. Bind to loopback or protected
Unix sockets and use SSH tunnels for remote access rather than publishing them.
Container loopback belongs to the container, not the host; explicitly reason
through each forwarding hop before adding Docker port publishing.

## Sharing files

Prefer a seed ISO or SSH/SCP for small inputs. 9p/virtiofs can expose host files
and have host/guest support and permissions requirements. Begin with a dedicated
read-only directory, not the user's home, SSH agent, Docker socket or credential
tree. virtiofs uses a separate daemon/socket and compatible shared memory
configuration. Shared directories can also interfere with migration/snapshots;
check device capabilities before depending on them.

Sources: [QEMU networking](https://www.qemu.org/docs/master/system/devices/net.html),
[invocation/forwarding/backends](https://www.qemu.org/docs/master/system/invocation.html),
[libvirt virsh](https://libvirt.org/manpages/virsh.html).
