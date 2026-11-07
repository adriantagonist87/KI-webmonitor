#!/usr/bin/env python3
"""Generate the single-file Portainer equivalent of compose.lan.yaml."""
from pathlib import Path


def render(source):
    changes = {
        '      ROLE: app\n': '      ROLE: app\n      KIWM_LAN_URL: http://${KIWM_LAN_IP:?LAN IPv4 required}:${KIWM_LAN_PORT:-8765}\n',
        '      BROWSER_PORT: ${BROWSER_PORT:-8766}\n':
            '      BROWSER_PORT: ${BROWSER_PORT:-8766}\n'
            '      KIWM_LAN_IP: ${KIWM_LAN_IP:?LAN IPv4 required}\n'
            '      KIWM_LAN_PORT: ${KIWM_LAN_PORT:-8765}\n'
            '      KIWM_LAN_SUBNET: ${KIWM_LAN_SUBNET:?Allowed LAN subnet required}\n',
        "      - '127.0.0.1:${BROWSER_PORT:-8766}:8081'\n":
            "      - '127.0.0.1:${BROWSER_PORT:-8766}:8081'\n"
            "      - '${KIWM_LAN_IP:?LAN IPv4 required}:${KIWM_LAN_PORT:-8765}:8083'\n",
    }
    for old, new in changes.items():
        if source.count(old) != 1:
            raise ValueError('Canonical Compose structure changed; inspect LAN overlay before regenerating.')
        source = source.replace(old, new)
    return '# Optional HTTP LAN stack. Configure KIWM_ADMIN_USERNAME and KIWM_ADMIN_PASSWORD.\n' + source


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    (root / 'portainer-lan-stack.yaml').write_text(render((root / 'compose.yaml').read_text()), newline='\n')
