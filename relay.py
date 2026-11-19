"""Byte-for-byte UDP relay: copies every received datagram to other listeners.

Each destination gets its own connected, non-blocking socket, so the hot path
in ui._udp_loop is a bare `sock.send(data)` per destination: no address parsing
or DNS lookups per packet, and a full send buffer drops the copy instead of
stalling the telemetry thread. Sends never go out of the listening socket: on
Windows an ICMP "port unreachable" for a dead destination would surface as
ConnectionResetError on that socket's next recvfrom and kill the UDP thread.
"""

import ipaddress
import socket


def open_forwarders(targets, listen_port):
    """Connect one UDP socket per (host, port). Returns (sockets, warnings).

    A destination that can't be resolved or reached is skipped with a warning,
    and so is one that would send packets back to our own port on this machine,
    which would loop every datagram forever.
    """
    sockets, warnings = [], []
    for host, port in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Resolves a hostname once, here, rather than on every send.
            sock.connect((host, port))
            peer_ip, peer_port = sock.getpeername()
            local_ip = sock.getsockname()[0]
        except OSError as e:
            sock.close()
            warnings.append(f"udp_forward: skipping {host}:{port}: {e}")
            continue
        if peer_port == listen_port and _is_this_machine(peer_ip, local_ip):
            sock.close()
            warnings.append(f"udp_forward: skipping {host}:{port}: it is this overlay's "
                            f"own port, every packet would loop back forever")
            continue
        sock.setblocking(False)
        sockets.append(sock)
    return sockets, warnings


def _is_this_machine(peer_ip, local_ip):
    # The kernel picks the destination itself as the source address when the
    # destination is one of this machine's own addresses (LAN IP included).
    ip = ipaddress.ip_address(peer_ip)
    return ip.is_loopback or ip.is_unspecified or peer_ip == local_ip
