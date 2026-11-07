#!/usr/bin/env python3
"""QEMU guest-agent client; synchronize the channel before issuing an RPC."""
import argparse
import json
import secrets
import socket
import time


class GuestAgent:
    def __init__(self, path, timeout=10):
        self.timeout = timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile('rb')
        ident = secrets.randbits(48)
        request = {'execute': 'guest-sync-delimited', 'arguments': {'id': ident}}
        self.socket.sendall(b'\xff' + json.dumps(request).encode() + b'\n')
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Guest-agent synchronization timed out')
            self.socket.settimeout(remaining)
            byte = self.stream.read(1)
            if not byte:
                raise EOFError('Guest agent closed during synchronization')
            if byte == b'\xff':
                response = self.read()
                if response.get('return') == ident:
                    break

    def read(self):
        line = self.stream.readline()
        if not line:
            raise EOFError('Guest-agent socket closed')
        return json.loads(line)

    def execute(self, command, arguments=None):
        self.socket.settimeout(self.timeout)
        request = {'execute': command}
        if arguments is not None:
            request['arguments'] = arguments
        self.socket.sendall(json.dumps(request).encode() + b'\n')
        response = self.read()
        if 'error' in response:
            raise RuntimeError(json.dumps(response['error']))
        return response['return']

    def close(self):
        self.stream.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('command')
    parser.add_argument('arguments', nargs='?', default='{}')
    args = parser.parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        parser.error('arguments must be a JSON object')
    with GuestAgent(args.socket, args.timeout) as client:
        print(json.dumps(client.execute(args.command, arguments), indent=2))


if __name__ == '__main__':
    main()
