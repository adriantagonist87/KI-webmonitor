"""Network-boundary regression checks without contacting the Mac or loading secrets."""
import ast
import ipaddress
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

# Execute the actual two pure policy functions without importing runtime stores.
tree = ast.parse(Path(__file__).with_name('gateway.py').read_text(encoding='utf-8'))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ('monitor_network', 'endpoint')]
namespace = {'ipaddress': ipaddress, 'os': os, 'socket': socket, 'urlsplit': urlsplit,
             'settings': lambda: {'omlx_url': 'http://local-inference.invalid:48173'}}
exec(compile(ast.Module(body=functions, type_ignores=[]), 'gateway.py', 'exec'), namespace)


class GatewayEndpointChecks(unittest.TestCase):
    def resolve(self, addresses, subnet='172.29.134.0/24'):
        results = [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM,
                    6, '', (ip, 48173)) for ip in addresses]
        with patch.dict(os.environ, {'MONITOR_SUBNET': subnet}), \
                patch.object(socket, 'getaddrinfo', return_value=results):
            return namespace['endpoint']()

    def test_local_inference_server_is_allowed(self):
        self.assertEqual(self.resolve(['192.168.50.8']), 'http://local-inference.invalid:48173/v1')

    def test_custom_stack_network_is_blocked(self):
        for subnet, ip in [('172.29.134.0/24', '172.29.134.20'),
                           ('172.29.135.0/24', '172.29.135.40'),
                           ('10.20.30.0/24', '10.20.30.2')]:
            with self.subTest(subnet=subnet), self.assertRaises(ValueError):
                self.resolve([ip], subnet)

    def test_mixed_dns_results_and_ipv4_mapped_addresses_are_blocked(self):
        for addresses in [['192.168.50.8', '172.29.134.20'], ['::ffff:172.29.134.20']]:
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                self.resolve(addresses)

    def test_public_loopback_and_link_local_are_blocked(self):
        for ip in ['8.8.8.8', '127.0.0.1', '169.254.169.254', '::1', '0.0.0.0']:
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                self.resolve([ip])

    def test_invalid_network_fails_closed(self):
        for subnet in ['invalid', '', '0.0.0.0/0', '172.29.134.20/24', '127.0.0.0/8', 'fd00::/64']:
            with self.subTest(subnet=subnet), self.assertRaises(ValueError):
                self.resolve(['192.168.50.8'], subnet)

    def test_default_network_remains_blocked(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError), \
                patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM,
                                                                 6, '', ('172.29.133.20', 48173))]):
            namespace['endpoint']()


if __name__ == '__main__':
    unittest.main()
