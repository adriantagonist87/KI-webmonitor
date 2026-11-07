#!/usr/bin/env python3
"""Create a password-protected HTTP LAN endpoint without changing other services."""
import argparse
import ipaddress
import os
from pathlib import Path
import tempfile

from bootstrap import env_file, plain_ancestors


def validate(address, subnet, port):
    ip = ipaddress.IPv4Address(address)
    network = ipaddress.IPv4Network(subnet, strict=True)
    private = any(network.subnet_of(ipaddress.ip_network(n)) for n in
                  ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if (not private or ip not in network or ip in (network.network_address, network.broadcast_address)
            or not 1024 <= port <= 65535):
        raise ValueError('Private LAN address, containing private subnet and port 1024-65535 required.')
    return str(ip), str(network), port


def setup(root, address, subnet, port):
    address, subnet, port = validate(address, subnet, port)
    root = Path(root)
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError('An absolute project directory is required.')
    plain_ancestors(root)
    if not root.is_dir() or os.geteuid() != 0:
        raise ValueError('Run as root in the existing Linux project directory.')
    values = {'KIWM_LAN_IP': address, 'KIWM_LAN_PORT': str(port), 'KIWM_LAN_SUBNET': subnet}
    path = root / '.env'
    existing = env_file(path)
    existing.update(values)
    # Preserve original formatting and comments outside the four LAN variables.
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition('=')[0] not in values]
    lines += [key + '=' + value for key, value in values.items()]
    descriptor, temporary = tempfile.mkstemp(prefix='.lan-env-', dir=root)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'LAN network ready: http://{address}:{port}/')
    print('Set KIWM_ADMIN_USERNAME and KIWM_ADMIN_PASSWORD in Portainer or private .env before enabling LAN.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--ip', required=True)
    parser.add_argument('--subnet', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        setup(args.root, args.ip, args.subnet, args.port)
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(str(error)) from None
