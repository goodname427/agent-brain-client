"""Synthetic content write failures; no device credentials, real memories or network."""

from contextlib import contextmanager, redirect_stderr
import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

from brain_lock import cache_locked as locked
from content_mcp import ContentParameterError, ContentServer, ContentToolUnavailable, enabled_tools
from content_cache import CacheBlocked, ContentAdapter
from remote_http import RequestStopped
import test_content_outbox as fixture


@contextmanager
def short_lock(path):
    with locked(path, timeout=0.01) as handle:
        yield handle


class WriteFailures(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server = ContentServer(self.f.adapter)
        self.request = self.f.request()

    def invoke(self):
        return self.server.invoke(
            "content_put",
            dict(self.request["params"], request_id=self.request["request_id"]),
        )["structuredContent"]

    def test_real_content_and_enqueue_lock_contention_is_retryable_and_saves_once(self):
        for filename, stage in [
            ("content.lock", "content-lock"),
            ("outbox-enqueue.lock", "outbox-lock"),
        ]:
            with self.subTest(lock=filename):
                with (
                    locked(self.f.cache / filename),
                    patch("content_cache.locked", short_lock),
                ):
                    result = self.invoke()
                self.assertEqual(
                    result["error"],
                    {
                        "code": "content_busy",
                        "retryable": True,
                        "stage": stage,
                        "exception_type": "LockTimeout",
                    },
                )
                self.assertEqual(result["request_id"], self.request["request_id"])
                self.assertFalse(result["persistence"]["durable"])
                self.assertEqual(self.f.cloud.calls, [])
                self.assertEqual(self.f.adapter.queue_status()["pending"], 0)
        result = self.invoke()  # Only this explicit identical retry sends the write.
        self.assertEqual(result["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.invoke()["result"], result["result"])
        self.assertEqual(self.f.cloud.puts, 1)

    def test_enqueue_fsync_failure_has_stage_and_type_without_secret_detail(self):
        stderr = io.StringIO()
        with (
            patch(
                "content_cache.os.fsync",
                side_effect=OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY"),
            ),
            redirect_stderr(stderr),
        ):
            result = self.invoke()
        self.assertEqual(
            result["error"],
            {
                "code": "content_adapter_blocked",
                "retryable": False,
                "stage": "outbox-enqueue",
                "exception_type": "OSError",
            },
        )
        self.assertEqual(self.f.cloud.calls, [])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result) + stderr.getvalue())
        with patch.object(
            self.f.adapter, "invoke", side_effect=ValueError("SYNTHETIC_PRIVATE")
        ):
            self.assertEqual(self.invoke()["error"]["stage"], "adapter-invoke")

    def test_lost_local_confirmation_reports_its_stage_and_exact_retry_is_idempotent(
        self,
    ):
        save = self.f.adapter._save

        def fail_confirmation(name, value):
            if name.startswith("outbox/") and value.get("state") == "confirmed":
                raise OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY")
            return save(name, value)

        with patch.object(self.f.adapter, "_save", side_effect=fail_confirmation):
            result = self.invoke()
        self.assertEqual(result["error"]["stage"], "outbox-confirmation")
        self.assertEqual(result["error"]["exception_type"], "OSError")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.f.adapter.queue_status()["pending"], 1)
        self.assertEqual(self.invoke()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 1)

    def test_healthy_32_entry_queue_does_not_block_new_write(self):
        for index in range(32):
            request = self.f.request(f"synthetic_previous_{index:04d}")
            request["params"]["id"] = f"synthetic-topic-{index}"
            self.f.adapter.invoke(request)
        self.assertEqual(self.f.adapter.queue_status()["confirmed"], 32)
        self.assertEqual(self.invoke()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 33)

    def test_terminal_conflict_is_preserved_without_blocking_a_fresh_cas_save(self):
        self.f.adapter.invoke(self.request)
        conflict = self.f.request("synthetic_old_conflict", body="Conflicting proposal")
        self.f.adapter.invoke(conflict)
        path=self.f.cache/"outbox"/(conflict["request_id"]+".json")
        before=path.read_bytes()
        revision=fixture.contract.envelope("memory","synthetic-topic",fixture.MEMORY)["revision"]
        self.request=self.f.request("synthetic_fresh_write",body="Fresh reviewed proposal",revision=revision)
        result=self.invoke()
        self.assertTrue(result["local_queue"]["server_saved"])
        self.assertEqual(result["local_queue"]["blocked_by"],[])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.f.cloud.puts,2)
        self.assertEqual(self.f.restart().queue_status()["conflict"],1)

    def test_blocked_predecessor_and_unrelated_target_are_distinguished(self):
        original = self.f.cloud.request

        def reject(request):
            if request["method"] == "content_put":
                return {"request_id": request["request_id"], "error": {"code": "invalid_request", "retryable": False}, "persistence": {"durable": False}}
            return original(request)

        self.f.cloud.request = reject
        self.invoke()
        self.f.cloud.request = original
        self.request = self.f.request("synthetic_waiting_blocked")
        result = self.invoke()
        self.assertEqual(result["local_queue"]["blocked_by"], [])
        self.assertTrue(result["local_queue"]["server_saved"])
        self.assertEqual(self.f.adapter.queue_status()["blocked"],1)
        self.assertEqual(self.f.cloud.puts, 1)
        self.request = self.f.request("synthetic_unrelated_write")
        self.request["params"]["id"] = "synthetic-unrelated"
        self.assertEqual(self.invoke()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 2)

    def test_confirmed_merge_clears_only_resolved_blockers_in_same_drain(self):
        self.f.adapter.invoke(self.request)
        conflict = self.f.request("synthetic_first_conflict", body="Local conflicting proposal")
        self.f.adapter.invoke(conflict)
        revision = fixture.contract.envelope("memory", "synthetic-topic", fixture.MEMORY)["revision"]
        merged = self.f.request("synthetic_reviewed_merge", body="Reviewed merged proposal", revision=revision)
        followup = self.f.request("synthetic_followup_write", body="Later proposal", revision=fixture.contract.envelope("memory", "synthetic-topic", merged["params"]["record"])["revision"])
        self.f.adapter._enqueue(merged, {"request_ids": [conflict["request_id"]], "source_keys": []})
        self.f.adapter._enqueue(followup)
        self.f.cloud.lose_ack = True
        self.f.adapter._drain()
        self.assertEqual(self.f.adapter.queue_status()["pending"], 2)
        self.assertEqual(self.f.adapter.queue_status()["conflict"], 1)
        # An exact replay recovers the lost receipt before releasing dependents.
        self.f.restart()._drain()
        self.assertEqual(self.f.adapter.queue_status()["pending"], 0)
        self.assertEqual(self.f.adapter.queue_status()["reconciled"], 1)
        self.assertEqual(self.f.cloud.puts, 3)
        for request in (conflict, merged, followup):
            row = self.f.adapter._load("outbox/" + request["request_id"] + ".json")
            self.assertEqual(row["request"], request)
        self.assertEqual(self.f.cloud.records[("memory", "synthetic-topic")], followup["params"]["record"])

    def test_offline_pending_does_not_claim_a_conflict(self):
        self.f.cloud.offline = True
        result = self.invoke()
        self.assertEqual(result["error"], {"code": "content_upload_queued", "retryable": True})
        self.assertEqual(result["local_queue"]["blocked_by"], [])
        self.assertIn("unconfirmed", result["local_queue"]["next_action"])

    def test_merge_does_not_release_unselected_conflict(self):
        self.f.adapter.invoke(self.request)
        first = self.f.request("synthetic_conflict_first")
        second = self.f.request("synthetic_conflict_second")
        self.f.adapter.invoke(first)
        # Stage two historical conflicts without bypassing the ordinary queue.
        row = self.f.adapter._enqueue(second)
        row.update(state="conflict", response={"request_id": second["request_id"], "error": {"code": "revision_conflict", "retryable": False}, "persistence": {"durable": False}})
        self.f.adapter._save("outbox/" + second["request_id"] + ".json", row)
        revision = fixture.contract.envelope("memory", "synthetic-topic", fixture.MEMORY)["revision"]
        merged = self.f.request("synthetic_partial_merge", revision=revision)
        result = self.f.adapter.invoke(merged, reconciles={"request_ids": [first["request_id"]], "source_keys": []})
        self.assertEqual(result["local_queue"]["blocked_by"], [])
        self.assertTrue(result["local_queue"]["server_saved"])
        self.assertEqual(self.f.cloud.puts, 2)
        self.assertEqual(self.f.adapter.queue_status()["conflict"], 1)
        self.assertEqual(self.f.adapter.inspect_queue(request_id=second["request_id"])["request"],second)

    def test_existing_pending_after_merge_keeps_its_original_cas(self):
        self.f.adapter.invoke(self.request)
        conflict = self.f.request("synthetic_prior_conflict")
        self.f.adapter.invoke(conflict)
        revision = fixture.contract.envelope("memory", "synthetic-topic", fixture.MEMORY)["revision"]
        waiting = self.f.request("synthetic_original_waiter", body="Original queued proposal", revision=revision)
        self.f.adapter._enqueue(waiting)
        merged = self.f.request("synthetic_explicit_merge", body="Explicit merged proposal", revision=revision)
        self.f.adapter.invoke(merged, reconciles={"request_ids": [conflict["request_id"]], "source_keys": []})
        row = self.f.adapter._load("outbox/" + waiting["request_id"] + ".json")
        self.assertEqual(row["request"], waiting)
        self.assertEqual(row["state"], "confirmed")
        self.assertEqual(self.f.cloud.records[("memory", "synthetic-topic")], waiting["params"]["record"])
        self.assertEqual(self.f.adapter.inspect_queue(request_id=merged["request_id"])["local_queue"]["state"],"conflict")
        self.assertEqual(self.f.cloud.puts, 2)

    def test_confirmed_merge_recovers_reconciliation_crash_before_followup(self):
        self.f.adapter.invoke(self.request)
        conflict = self.f.request("synthetic_crash_conflict")
        self.f.adapter.invoke(conflict)
        revision = fixture.contract.envelope("memory", "synthetic-topic", fixture.MEMORY)["revision"]
        merged = self.f.request("synthetic_crash_merge", body="Reviewed merge", revision=revision)
        followup = self.f.request("synthetic_after_crash", body="Later write", revision=fixture.contract.envelope("memory", "synthetic-topic", merged["params"]["record"])["revision"])
        self.f.adapter._enqueue(merged, {"request_ids": [conflict["request_id"]], "source_keys": []})
        self.f.adapter._enqueue(followup)
        finish = self.f.adapter._finish_reconciliation

        def interrupted(row):
            if row["request"]["request_id"] == merged["request_id"]:
                raise SystemExit("Synthetic crash after confirmed receipt")
            finish(row)

        with patch.object(self.f.adapter, "_finish_reconciliation", interrupted):
            with self.assertRaises(SystemExit):
                self.f.adapter._drain()
        self.f.restart()._drain()
        self.assertEqual(self.f.adapter.queue_status()["pending"], 0)
        self.assertEqual(self.f.adapter.queue_status()["reconciled"], 1)
        self.assertEqual(self.f.cloud.puts, 3)

    def test_failed_error_response_save_is_not_a_server_confirmation(self):
        original = self.f.cloud.request
        save = self.f.adapter._save
        for code in ("invalid_request", "revision_conflict"):
            with self.subTest(code=code):

                def deny(request):
                    if request["method"] == "content_put":
                        return {
                            "request_id": request["request_id"],
                            "error": {"code": code, "retryable": False},
                            "persistence": {"durable": False},
                        }
                    return original(request)

                def fail_response(name, value):
                    if name.startswith("outbox/") and value.get("state") in (
                        "blocked",
                        "conflict",
                    ):
                        raise OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY")
                    return save(name, value)

                with (
                    patch.object(self.f.cloud, "request", side_effect=deny),
                    patch.object(self.f.adapter, "_save", side_effect=fail_response),
                ):
                    result = self.invoke()
                self.assertEqual(result["error"]["stage"], "outbox-response")
                self.assertEqual(result["error"]["exception_type"], "OSError")
                self.assertFalse(result["persistence"]["durable"])
                self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
                self.assertEqual(self.f.cloud.puts, 0)
                self.assertEqual(self.f.adapter.queue_status()["pending"], 1)

    def test_unknown_exception_name_and_message_are_not_exposed_or_reclassified_as_parameters(
        self,
    ):
        private_error = type("SYNTHETIC_PRIVATE_TYPE", (Exception,), {})
        with patch.object(
            self.f.adapter,
            "invoke",
            side_effect=private_error("SYNTHETIC_PRIVATE_BODY"),
        ):
            result = self.invoke()
        self.assertEqual(result["error"]["stage"], "adapter-invoke")
        self.assertEqual(result["error"]["exception_type"], "Exception")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        with self.assertRaises(ContentParameterError):
            self.server.invoke("content_put", {})
        with patch.object(
            self.f.adapter, "invoke", side_effect=RequestStopped("request_cancelled")
        ):
            with self.assertRaises(RequestStopped):
                self.invoke()
        # Diagnostics from a previous failed call must not bleed into this one.
        with patch.object(
            self.f.adapter, "invoke", side_effect=ValueError("SYNTHETIC_PRIVATE")
        ):
            self.assertEqual(self.invoke()["error"]["stage"], "adapter-invoke")


class CollectionCapacity(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # Individually small, valid records; the complete collection is >1 MiB.
        for index in range(96):
            self.f.cloud.records['memory', 'synthetic-large-' + str(index)] = dict(
                fixture.MEMORY, body=('Synthetic collection 中文\n' * 600) + str(index))

    def restart(self):
        return ContentAdapter(self.f.home, self.f.cache, fixture.POLICY, self.f.cloud,
                              codex_home=self.f.home / '.codex')

    def files(self):
        return {p.name: p.read_bytes() for p in
                (self.f.home / '.config/agent-brain-client/content/memory').iterdir()}

    def test_large_collection_save_restart_and_offline_read(self):
        result = self.f.adapter.invoke(self.f.request())
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(result['local_refresh']['status'], 'content-refreshed')
        active = json.loads((self.f.cache / 'active.json').read_bytes())
        snapshot = self.f.cache / 'snapshots' / (active['commit'] + '.json')
        self.assertGreater(snapshot.stat().st_size, 1048576)
        before = self.files()
        outbox = {p.name: p.read_bytes() for p in (self.f.cache / 'outbox').iterdir()}
        self.f.cloud.calls.clear()
        adapter = self.restart()
        self.assertEqual(adapter.refresh()['status'], 'content-current')
        self.assertFalse(any(r['method'] in ('content_read', 'content_put') for r in self.f.cloud.calls))
        self.assertEqual(self.files(), before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.f.cache / 'outbox').iterdir()}, outbox)
        self.f.cloud.offline = True
        reply = adapter.invoke({'request_id': 'synthetic_offline_collection', 'method': 'content_read',
                                'params': {'kind': 'memory', 'id': 'synthetic-large-0'}})
        self.assertTrue(reply['local_cache']['stale'])
        self.assertEqual(reply['result']['record'], self.f.cloud.records['memory', 'synthetic-large-0'])

    def test_large_recovery_journal_rolls_back_and_survives_restart(self):
        self.assertEqual(self.f.adapter.refresh()['status'], 'content-refreshed')
        before = self.files()
        active = (self.f.cache / 'active.json').read_bytes()
        for record in self.f.cloud.records.values():
            record['body'] = record['body'].replace('Synthetic collection', 'Synthetic replacement')
        self.f.cloud.puts += 1
        save = self.f.adapter._save

        def fail(name, value):
            if name.startswith('snapshots/'):
                raise OSError('Synthetic snapshot persistence failure')
            return save(name, value)

        with patch.object(self.f.adapter, '_save', side_effect=fail):
            result = self.f.adapter.refresh()
        self.assertEqual(result['stage'], 'snapshot-save')
        self.assertEqual(result['status'], 'content-refresh-blocked')
        self.assertGreater((self.f.cache / 'transaction.json').stat().st_size, 3 * 1048576)
        self.assertEqual(json.loads((self.f.cache / 'transaction.json').read_bytes())['phase'], 'rolled-back')
        self.assertEqual((self.f.cache / 'active.json').read_bytes(), active)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.restart().refresh()['status'], 'content-refreshed')
        self.assertNotEqual(self.files(), before)

    def test_record_byte_limit_preserves_mapping_and_catalog_can_exceed_one_page(self):
        self.assertEqual(self.f.adapter.refresh()['status'], 'content-refreshed')
        before = self.files()
        active = (self.f.cache / 'active.json').read_bytes()
        self.f.cloud.records['memory', 'synthetic-too-large'] = dict(fixture.MEMORY, body='x' * 65536)
        self.f.cloud.puts += 1
        result = self.restart().refresh()
        self.assertEqual(result['stage'], 'remote-snapshot')
        self.assertEqual(result['status'], 'content-refresh-blocked')
        self.assertEqual((self.f.cache / 'active.json').read_bytes(), active)
        self.assertEqual(self.files(), before)
        del self.f.cloud.records['memory', 'synthetic-too-large']
        for index in range(257):
            self.f.cloud.records['memory', 'synthetic-count-' + str(index)] = copy.deepcopy(fixture.MEMORY)
        result = self.restart().refresh()
        self.assertEqual(result['status'], 'content-refreshed')
        self.assertNotEqual((self.f.cache / 'active.json').read_bytes(), active)

    def test_interrupted_large_transaction_recovers_before_remote_fetch(self):
        self.assertEqual(self.f.adapter.refresh()['status'], 'content-refreshed')
        before = self.files()
        active = (self.f.cache / 'active.json').read_bytes()
        for record in self.f.cloud.records.values():
            record['body'] = record['body'].replace('Synthetic collection', 'Synthetic interrupted')
        self.f.cloud.puts += 1
        save = self.f.adapter._save

        def interrupted(name, value):
            if name.startswith('snapshots/'):
                raise SystemExit('Synthetic process exit before snapshot persistence')
            return save(name, value)

        with patch.object(self.f.adapter, '_save', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.f.adapter.refresh()
        self.assertGreater((self.f.cache / 'transaction.json').stat().st_size, 3 * 1048576)
        self.assertEqual(json.loads((self.f.cache / 'transaction.json').read_bytes())['phase'], 'applying')
        self.assertNotEqual(self.files(), before)
        adapter = self.restart()
        with patch.object(adapter, '_fetch', side_effect=OSError('Synthetic disconnected after recovery')):
            result = adapter.refresh()
        self.assertEqual(result['stage'], 'remote-snapshot')
        self.assertEqual(json.loads((self.f.cache / 'transaction.json').read_bytes())['phase'], 'rolled-back')
        self.assertEqual((self.f.cache / 'active.json').read_bytes(), active)
        self.assertEqual(self.files(), before)


class QueueRecovery(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server = ContentServer(self.f.adapter)

    def inspect(self, **arguments):
        return self.server.invoke('content_queue_inspect', arguments)['structuredContent']

    def conflict(self):
        self.f.adapter.invoke(self.f.request())
        request = self.f.request('synthetic_conflict_inspect', body='Retain this unique local proposal')
        self.f.adapter.invoke(request)
        return request

    def files(self):
        return {p.name: p.read_bytes() for p in (self.f.cache / 'outbox').iterdir()}

    def test_inspection_exposes_exact_evidence_without_replay_or_changes(self):
        conflict = self.conflict()
        waiting = self.f.request('synthetic_inspect_waiter', body='Another retained proposal')
        self.f.adapter.invoke(waiting)
        before = self.files()
        self.f.cloud.calls.clear()
        self.f.cloud.offline = True
        self.server = ContentServer(self.f.restart())
        listing = self.inspect()
        self.assertEqual(listing['counts']['conflict'], 2)
        entry = next(e for e in listing['entries'] if e['request_id'] == waiting['request_id'])
        self.assertEqual(entry['local_queue']['blocked_by'], [])
        self.assertEqual(entry['local_queue']['state'],'conflict')
        self.assertNotIn('unique local proposal', json.dumps(listing))
        self.assertNotIn(str(self.f.cache), json.dumps(listing))
        exact = self.inspect(request_id=conflict['request_id'])
        self.assertEqual(exact['request'], conflict)
        self.assertEqual(exact['remote_snapshot']['record'], fixture.MEMORY)
        self.assertEqual(exact['response']['error']['code'], 'revision_conflict')
        self.assertFalse(exact['remote_snapshot_current'])
        self.assertFalse(exact['data_transferred'])
        self.assertEqual(self.f.cloud.calls, [])
        self.assertEqual(self.files(), before)

    def test_pages_bind_to_snapshot_and_reject_invalid_or_stale_cursors(self):
        for index in range(65):
            request = self.f.request(f'synthetic_inspection_{index:04d}')
            request['params']['id'] = f'synthetic-topic-{index}'
            self.f.adapter._enqueue(request)
        first = self.inspect()
        second = self.inspect(cursor=first['next_cursor'])
        self.assertEqual(len(first['entries']), 64)
        self.assertEqual(len(second['entries']), 1)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({e['request_id'] for e in first['entries'] + second['entries']}), 65)
        self.f.adapter._enqueue(self.f.request('synthetic_cursor_changed'))
        with self.assertRaises(ContentParameterError):
            self.inspect(cursor=first['next_cursor'])
        for arguments in ({'request_id': '../private'}, {'cursor': None}, {'request_id': None},
                          {'cursor': 'bad'}, {'request_id': 'synthetic_inspection_0000', 'cursor': first['next_cursor']}):
            with self.subTest(arguments=arguments), self.assertRaises(ContentParameterError):
                self.server.invoke('content_queue_inspect', arguments)
        self.assertEqual(self.inspect(request_id='synthetic_not_retained')['status'], 'not-found')
        self.assertEqual(self.f.cloud.calls, [])

    def test_no_write_grant_cannot_offer_or_invoke_inspection(self):
        grant = copy.deepcopy(self.f.cloud.grant)
        grant['scopes'] = ['read']
        self.assertNotIn('content_queue_inspect', enabled_tools(fixture.POLICY, grant))
        self.f.cloud.grant = grant
        self.server.state = 'ready'
        listing = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertNotIn('content_queue_inspect', {t['name'] for t in listing['tools']})
        with self.assertRaises(ContentToolUnavailable):
            self.inspect()
        self.assertEqual(self.f.cloud.calls, [])

    def test_identity_drift_preserves_queue_and_exposes_no_body(self):
        self.conflict()
        before = self.files()
        self.f.adapter.queue_sha = 'a' * 64
        with self.assertRaises(CacheBlocked):
            self.f.adapter.inspect_queue()
        self.assertEqual(self.files(), before)

    def test_older_waiter_keeps_original_cas_instead_of_waiting_for_a_future_resolver(self):
        conflict = self.conflict()
        remote = self.f.adapter._load('outbox/' + conflict['request_id'] + '.json')['remote']
        merged = self.f.request('synthetic_inspect_resolver', body='Merge of remote and local', revision=remote['revision'])
        waiter = self.f.request('synthetic_older_waiter', body='Later reviewed proposal',
                                revision=fixture.contract.envelope('memory', 'synthetic-topic', merged['params']['record'])['revision'])
        self.f.adapter._enqueue(waiter)
        self.f.adapter._enqueue(merged, {'request_ids': [conflict['request_id']], 'source_keys': []})
        self.f.cloud.calls.clear()
        self.f.restart()._drain()
        writes = [r for r in self.f.cloud.calls if r['method'] == 'content_put']
        self.assertEqual(writes, [waiter, merged])
        self.assertEqual(self.f.adapter.queue_status()['pending'], 0)
        self.assertEqual(self.f.adapter.queue_status()['reconciled'], 1)
        self.assertEqual(self.f.cloud.records['memory', 'synthetic-topic'], merged['params']['record'])
        self.assertEqual(self.inspect(request_id=waiter['request_id'])['local_queue']['state'],'conflict')
        original = self.inspect(request_id=conflict['request_id'])
        self.assertEqual(original['request'], conflict)
        self.assertEqual(original['local_queue']['resolution_request_id'], merged['request_id'])

    def test_uncertain_waiter_stops_drain_until_exact_retry(self):
        conflict = self.conflict()
        remote = self.f.adapter._load('outbox/' + conflict['request_id'] + '.json')['remote']
        merged = self.f.request('synthetic_uncertain_resolver', body='Reviewed merge', revision=remote['revision'])
        waiter = self.f.request('synthetic_uncertain_waiter', body='Retained waiter',
                                revision=fixture.contract.envelope('memory', 'synthetic-topic', merged['params']['record'])['revision'])
        refs = {'request_ids': [conflict['request_id']], 'source_keys': []}
        self.f.adapter._enqueue(merged, refs)
        self.f.adapter._enqueue(waiter)
        original = self.f.cloud.request

        def lose_waiter_ack(request):
            if request['request_id'] == waiter['request_id']:
                self.f.cloud.lose_ack = True
            return original(request)

        self.f.cloud.calls.clear()
        with patch.object(self.f.cloud, 'request', side_effect=lose_waiter_ack):
            result = self.f.adapter.invoke(merged, reconciles=refs)
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(result['local_refresh']['queue']['pending'], 1)
        self.assertEqual([r for r in self.f.cloud.calls if r['method'] == 'content_put'], [merged, waiter])
        self.assertEqual(self.f.cloud.puts, 3)
        exact = self.inspect(request_id=waiter['request_id'])
        self.assertEqual(exact['local_queue']['state'], 'pending')
        self.assertFalse(exact['local_queue']['server_saved'])
        # A pending outcome cannot be declared resolved or silently replaced.
        replacement = self.f.request('synthetic_unsafe_replacement', revision=remote['revision'])
        with self.assertRaises(CacheBlocked):
            self.f.adapter._enqueue(replacement, {'request_ids': [waiter['request_id']], 'source_keys': []})
        result = self.f.restart().invoke(exact['request'], reconciles=exact['reconciles'])
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(self.f.cloud.puts, 3)


def load_tests(loader, tests, pattern):
    # The established Windows CI entry runs this suite explicitly. Include
    # shared activation/UI contracts there while Mac discovery loads them once.
    if sys.platform == 'win32':
        import test_content_activation
        tests.addTests(loader.loadTestsFromModule(test_content_activation))
    return tests


if __name__ == "__main__":
    unittest.main()
