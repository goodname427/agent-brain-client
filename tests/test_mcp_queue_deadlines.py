"""Synthetic queue stalls must return before the active durable write finishes."""

import queue
import threading
import unittest

from agent_brain_client.interfaces.mcp.server import serve
from test_content_read_requests import Source, Output, call


class QueueDeadlines(unittest.TestCase):
    def start(self, **options):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        fixture = self

        class Server:
            def handle(self, request):
                name = request['params']['name']
                fixture.calls.append((request['id'], name))
                if name == 'content_put':
                    fixture.started.set()
                    fixture.release.wait(3)
                return {'isError': False}

        self.source, self.output = Source(), Output()
        worker = threading.Thread(target=serve, args=(Server(), self.source, self.output), kwargs=options)
        worker.start()

        def close():
            self.release.set()
            self.source.end()
            worker.join(3)
            self.assertFalse(worker.is_alive())

        self.addCleanup(close)
        self.source.send(call(100, 'content_put'))
        self.assertTrue(self.started.wait(1))

    def response_before_write_finishes(self):
        try:
            response = self.output.responses.get(timeout=0.7)
        except queue.Empty:
            self.fail('Queued request did not reply while the durable write was still active')
        self.assertFalse(self.release.is_set())
        self.assertEqual(self.calls, [(100, 'content_put')])
        return response

    def test_query_read_and_catalog_deadlines_reply_while_write_is_still_active(self):
        self.start(request_timeout=0.1)
        for identifier, name in enumerate(('memory_query', 'content_read', 'content_catalog'), 1):
            self.source.send(call(identifier, name))
        replies = [self.response_before_write_finishes() for _ in range(3)]
        self.assertEqual({r['id'] for r in replies}, {1, 2, 3})
        for reply in replies:
            self.assertEqual(reply['error']['code'], -32001)
            self.assertEqual(reply['error']['data']['stage'], 'queue')
        self.release.set()
        self.assertEqual(self.output.get()['id'], 100)
        self.source.send(call(4))
        self.assertEqual(self.output.get()['id'], 4)
        self.assertEqual(self.calls, [(100, 'content_put'), (4, 'content_read')])
        with self.assertRaises(queue.Empty):
            self.output.responses.get(timeout=0.05)

    def test_cancelled_queue_entries_release_capacity_and_reused_ids_exactly_once(self):
        self.start(request_timeout=2, max_pending=2)
        for _ in range(6):
            self.source.send(call(1))
            self.source.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                              'params': {'requestId': 1}})
            reply = self.response_before_write_finishes()
            self.assertEqual(reply['id'], 1)
            self.assertEqual(reply['error']['code'], -32800)
            self.assertEqual(reply['error']['data']['stage'], 'queue')
        self.source.send(call(1))
        self.release.set()
        replies = [self.output.get() for _ in range(2)]
        self.assertEqual({r['id'] for r in replies}, {1, 100})
        self.assertTrue(all('result' in r for r in replies))
        self.assertEqual(self.calls, [(100, 'content_put'), (1, 'content_read')])

    def test_queued_write_deadline_does_not_start_it_or_cancel_active_write(self):
        self.start(request_timeout=0.1)
        self.source.send(call(1, 'content_put'))
        reply = self.response_before_write_finishes()
        self.assertEqual(reply['id'], 1)
        self.assertEqual(reply['error']['code'], -32001)
        self.release.set()
        self.assertEqual(self.output.get()['id'], 100)
        self.assertEqual(self.calls, [(100, 'content_put')])


if __name__ == '__main__':
    unittest.main()
