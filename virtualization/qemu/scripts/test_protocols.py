"""Protocol edge cases that are hard to force reliably with a real guest."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from qmp import QMP
from qga import GuestAgent


class ProtocolTests(unittest.TestCase):
    def server(self, handler):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'control.sock'
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)
        errors = []

        def serve():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    with connection.makefile('rb') as stream:
                        handler(connection, stream)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        def finished():
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.addCleanup(finished)
        return path

    def test_qmp_interleaved_events_and_errors(self):
        def handler(connection, stream):
            connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
            for index in range(3):
                request = json.loads(stream.readline())
                connection.sendall(b'{"event":"STOP","data":{}}\r\n')
                response = {'id':request['id']}
                if index == 2:
                    response['error'] = {'class':'CommandNotFound', 'desc':'unknown'}
                else:
                    response['return'] = {} if index == 0 else {'status':'paused'}
                connection.sendall(json.dumps(response).encode()+b'\r\n')
        with QMP(self.server(handler), timeout=1) as client:
            self.assertEqual(client.execute('query-status'), {'status':'paused'})
            with self.assertRaisesRegex(RuntimeError, 'CommandNotFound'):
                client.execute('unknown')
            self.assertEqual(len(client.events), 3)
            self.assertEqual(client.wait_event('STOP')['event'], 'STOP')
            self.assertEqual(len(client.events), 2)

    def test_qga_discards_stale_bytes_before_matching_sync(self):
        def handler(connection, stream):
            request = json.loads(stream.readline().lstrip(b'\xff'))
            connection.sendall(b'stale partial response\n\xff')
            connection.sendall(json.dumps({'return':request['arguments']['id']}).encode()+b'\n')
            self.assertEqual(json.loads(stream.readline())['execute'], 'guest-ping')
            connection.sendall(b'{"return":{}}\n')
        with GuestAgent(self.server(handler), timeout=1) as client:
            self.assertEqual(client.execute('guest-ping'), {})


if __name__ == '__main__':
    unittest.main()
