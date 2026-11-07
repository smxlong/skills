#!/usr/bin/env python3
"""Small synchronous QMP client: Unix socket, IDs, events, bounded waits."""
import argparse
import json
import socket
import time


class QMP:
    def __init__(self, path, timeout=10):
        self.timeout = timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile('rb')
        self.events = []
        self.sequence = 0
        greeting = self.read()
        if 'QMP' not in greeting:
            self.close()
            raise RuntimeError(f'Not a QMP greeting: {greeting}')
        self.version = greeting['QMP']['version']
        self.execute('qmp_capabilities')

    def read(self):
        line = self.stream.readline()
        if not line:
            raise EOFError('QMP socket closed')
        return json.loads(line)

    def execute(self, command, arguments=None):
        self.sequence += 1
        ident = self.sequence
        request = {'execute': command, 'id': ident}
        if arguments is not None:
            request['arguments'] = arguments
        self.socket.sendall((json.dumps(request) + '\r\n').encode())
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No QMP response to {command}')
            self.socket.settimeout(remaining)
            message = self.read()
            if 'event' in message:
                self.events.append(message)
                continue
            if message.get('id') != ident:
                raise RuntimeError(f'Unexpected QMP response ID: {message}')
            if 'error' in message:
                raise RuntimeError(json.dumps(message['error']))
            if 'return' not in message:
                raise RuntimeError(f'Malformed QMP response: {message}')
            return message['return']

    def close(self):
        self.stream.close()
        self.socket.close()

    def wait_event(self, name, predicate=None):
        """Wait for asynchronous completion, retaining unrelated events."""
        predicate = predicate or (lambda event: True)
        deadline = time.monotonic() + self.timeout
        while True:
            for index, event in enumerate(self.events):
                if event.get('event') == name and predicate(event):
                    return self.events.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No QMP event {name}')
            self.socket.settimeout(remaining)
            event = self.read()
            if 'event' not in event:
                raise RuntimeError(f'Unexpected response while waiting for event: {event}')
            self.events.append(event)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('command')
    parser.add_argument('arguments', nargs='?', default='{}', help='JSON object')
    args = parser.parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        parser.error('arguments must be a JSON object')
    with QMP(args.socket, args.timeout) as client:
        result = client.execute(args.command, arguments)
        print(json.dumps({'return': result, 'events': client.events}, indent=2))


if __name__ == '__main__':
    main()
