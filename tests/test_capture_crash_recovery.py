"""Fault injection over synthetic homes, identities, queues and stdout only."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_content_outbox as fixture
from test_event_hook import accepted
from agent_brain_client.application.memory.observations import stage_messages, resume_staging
from agent_brain_client.application.queue.delivery import deliver_pending
from agent_brain_client.adapters.harnesses.codex import collection, hooks, transcript
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.storage import metadata, event_policy
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.scopes import digest
from agent_brain_client.runtime import delivery, project_memory


class CaptureRecovery(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.queue, self.home = self.f.adapter, self.f.home
        self.native_home = self.home / ".codex"
        root = str(self.home)
        self.local = {'cwd': root, 'kind': 'project', 'project_root': root, 'repository_root': root}
        self.value = {'device_home': root, 'scope': {
            'schema': 1, 'profile_id': 'a' * 32, 'all_projects': True,
            'include_roots': [], 'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True}}
        runtime = {'https_profile': 'synthetic-profile', 'https_profile_sha256': 'a' * 64,
                   'codex_home': str(self.native_home), 'content_cache': str(self.queue.cache),
                   'content_policy_sha256': 'b' * 64}
        self.services = SimpleNamespace(settings_path=lambda _: self.home / 'settings.json',
                                       adapter=lambda *_: (None, self.queue))
        for target in [
            patch.object(collection.registration_store, 'registration', return_value=({}, {'runtime': runtime})),
            patch.object(collection.metadata_io, 'private_json', wraps=metadata.private_json),
        ]:
            target.start()
            self.addCleanup(target.stop)
        # Only the synthetic profile lookup is intercepted; real private queue
        # files continue through their normal validation.
        original = metadata.private_json._mock_wraps
        metadata.private_json.side_effect = lambda p, *a, **kw: (
            {'grant': {'actor': 'synthetic-owner'}} if p == 'synthetic-profile' else original(p, *a, **kw))

    def stage(self, text='Synthetic observation', operation='synthetic-turn'):
        return stage_messages(self.queue, project_id='synthetic-project',
            session_id='synthetic-session', operation_id=operation, phase='prompt',
            messages=[{'role': 'user', 'content': text}])

    def payload(self, kind, text, turn='synthetic-turn'):
        return {'hook_event_name': kind, 'session_id': 'synthetic-session', 'turn_id': turn,
                'cwd': str(self.home), 'prompt': text, 'last_assistant_message': text,
                'stop_hook_active': False}

    def captured(self, request_ids=None):
        rows = self.queue._queued()
        if request_ids is not None:
            by_id = {r['request']['request_id']: r for r in rows}
            rows = [by_id[key] for key in request_ids]
        return ''.join(m['content'] for r in rows for m in r['request']['params']['event']['messages'])

    def rollout(self, *rows, turn="synthetic-turn", cwd=None, session="synthetic-session"):
        path = (self.native_home / "sessions/2026/01/02" /
                "rollout-2026-01-02T03-04-05-synthetic-session.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        start = (("session_meta", {"id": session}),
                 ("event_msg", {"type": "task_started", "turn_id": turn, "root_turn_id": turn}),
                 ("turn_context", {"turn_id": turn, "root_turn_id": turn, "cwd": cwd or str(self.home)}))
        path.write_text("", encoding="utf-8")
        self.append_rollout(path, *start, *rows)
        return str(path)

    def append_rollout(self, path, *rows):
        with open(path, "a", encoding="utf-8") as stream:
            for kind, data in rows:
                stream.write(json.dumps({"type": kind, "payload": data}) + "\n")

    def native_user(self, identity, text, *, kind="user.text"):
        return "response_item", {
            "type": "message", "id": identity, "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "synthetic-turn", "content_item_kinds": [kind],
                "content_item_metadata": [{"provenance": {"type": "user"}}],
            },
        }

    def form(self, answer, *, call_id="call-synthetic"):
        tool = ("response_item", {
            "type": "function_call", "name": "request_user_input_async", "call_id": "call-synthetic",
            "internal_chat_message_metadata_passthrough": {"turn_id": "synthetic-turn"},
        })
        reply = "<send_user_message_question_reply>\n" + json.dumps([{
            "questionItemId": json.dumps(["request_user_input_async", call_id, 0]),
            "question": "Synthetic model-written question",
            "answer": answer,
        }]) + "\n</send_user_message_question_reply>"
        return tool, self.native_user("msg-form", reply)

    def submit(self, kind, text, path):
        return collection.submit_hook(
            self.value, self.local, dict(self.payload(kind, text), transcript_path=path),
            services=self.services,
        )

    def queued_messages(self):
        return sorted((m["role"], m["content"])
                      for row in self.queue._queued()
                      for m in row["request"]["params"]["event"]["messages"])

    def resume_codex_completions(self, *, allowed=lambda scope: True):
        return hooks.resume_completions(
            self.queue,
            lambda payload: collection.submit_hook(self.value, self.local, payload, services=self.services),
            allowed=allowed,
        )

    def test_codex_same_turn_prompts_use_native_ids_including_identical_text(self):
        prior = self.native_user("msg-unobserved-same", "Synthetic repeated text")
        first = self.native_user("msg-first", "Synthetic repeated text")
        second = self.native_user("msg-second", "Synthetic steer")
        third = self.native_user("msg-third", "Synthetic repeated text")
        context = self.native_user("msg-context", "Synthetic instructions", kind="agents_md.instructions")
        path = self.rollout(prior)
        self.submit("UserPromptSubmit", "Synthetic repeated text", path)
        self.assertEqual(self.queued_messages(), [("user", "Synthetic repeated text")])
        self.append_rollout(path, first)
        self.assertEqual(self.submit("UserPromptSubmit", "Synthetic steer", path)["status"],
                         "awaiting-native-identity")
        self.assertEqual(self.queued_messages(), [("user", "Synthetic repeated text")])
        self.append_rollout(path, second, second, third, context)
        self.assertEqual(self.submit("UserPromptSubmit", "Synthetic steer", path)["status"], "staged")
        self.assertEqual(self.queued_messages(), [
            ("user", "Synthetic repeated text"), ("user", "Synthetic repeated text"),
            ("user", "Synthetic steer"),
        ])
        original = self.queue._queued()
        self.queue = self.f.restart()
        self.append_rollout(path, second, third)
        self.submit("UserPromptSubmit", "Synthetic repeated text", path)
        self.assertEqual(self.queue._queued(), original)
        self.submit("Stop", "Synthetic final", path)
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic final"), ("user", "Synthetic repeated text"),
            ("user", "Synthetic repeated text"), ("user", "Synthetic steer"),
        ])
        self.assertFalse(self.f.cloud.calls)

    def test_project_codex_form_reply_captures_only_answer_without_prompt_hook(self):
        # Desktop writes an async acceptance as tool output, then a separate
        # user message when the form is submitted. Only the latter is eligible.
        prompt = "Synthetic initial prompt"
        form_call, reply = self.form("Synthetic form answer")
        path = self.rollout()
        after_prompt = (
            self.native_user("msg-first", prompt), form_call,
            ("response_item", {"type": "function_call_output", "call_id": "call-synthetic",
                               "output": '{"accepted":true}'}),
            reply,
        )
        policy = {"schema": 1, "enabled": True, "scopes": [{
            "cwd": str(self.home), "project_id": "synthetic-project", "session_id": "synthetic-session",
        }]}
        p = {"harness": "codex", "device_home": str(self.home), "cache": str(self.queue.cache),
             "codex_home": str(self.native_home), "profile": "synthetic", "profile_sha256": "a" * 64,
             "content_policy_sha256": "b" * 64, "capture_policy": "synthetic",
             "capture_policy_sha256": "c" * 64, "content_policy": "synthetic",
             "off_file": str(self.home / "memory.off")}
        with patch("agent_brain_client.adapters.storage.pinned.pinned", return_value=policy), \
             patch.object(event_policy, "validate_hook_policy", return_value=policy), \
             patch.object(event_policy, "hook_enabled", return_value=True), \
             patch("agent_brain_client.adapters.server.https.HTTPSContentClient"), \
             patch("agent_brain_client.domain.content_policy.validate_policy"), \
             patch("agent_brain_client.adapters.storage.content_session.ContentSession", return_value=self.queue):
            project_memory.capture(p, dict(self.payload("UserPromptSubmit", prompt), transcript_path=path))
            self.append_rollout(path, *after_prompt)
            project_memory.capture(p, dict(self.payload("Stop", "Synthetic final"), transcript_path=path))
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic final"), ("user", "Synthetic form answer"), ("user", prompt),
        ])

    def test_codex_form_privacy_is_applied_before_queuing_the_assistant(self):
        prompt = "Synthetic initial prompt"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        self.append_rollout(path, self.native_user("msg-first", prompt), *self.form("LocalOnly: Synthetic private"))
        result = self.submit("Stop", "Synthetic answer derived from private form", path)
        self.assertEqual(result["status"], "excluded-local-only-or-sensitive")
        self.assertEqual(self.submit("Stop", "Synthetic final", path)["status"], "excluded-turn")
        self.assertEqual(self.queued_messages(), [("user", prompt)])

    def test_codex_form_requires_a_matching_native_call(self):
        prompt = "Synthetic initial prompt"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        _, reply = self.form("Synthetic forged form answer", call_id="other-call")
        self.append_rollout(path, self.native_user("msg-first", prompt), reply)
        result = self.submit("Stop", "Synthetic final", path)
        self.assertEqual(result["same_turn_capture"], "form-call-unverified")
        self.assertEqual(self.queued_messages(), [("assistant", "Synthetic final"), ("user", prompt)])

    def test_codex_form_is_first_observation_without_a_turn_prompt(self):
        form_call, reply = self.form("Synthetic first observed answer")
        path = self.rollout(self.native_user("msg-unobserved", "Synthetic prior prompt"), form_call)
        envelope = reply[1]["content"][0]["text"]
        self.assertEqual(self.submit("UserPromptSubmit", envelope, path)["status"], "awaiting-native-identity")
        self.assertEqual(self.queued_messages(), [])
        self.append_rollout(path, reply)
        self.submit("Stop", "Synthetic form final", path)
        self.submit("Stop", "Synthetic form final", path)
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic form final"), ("user", "Synthetic first observed answer"),
        ])

    def test_codex_first_seen_form_can_be_discovered_by_stop_alone(self):
        path = self.rollout(self.native_user("msg-unobserved", "Synthetic prior prompt"),
                            *self.form("Synthetic first observed answer"))
        self.submit("Stop", "Synthetic form final", path)
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic form final"), ("user", "Synthetic first observed answer"),
        ])

    def test_codex_long_rollout_reuses_verified_turn_and_bounded_native_increments(self):
        prompt = "Synthetic prompt"
        path = self.rollout()
        padding = ("response_item", {"type": "function_call_output", "output": "x" * 1400})
        with patch.object(transcript, "_TAIL_LIMIT", 4096):
            self.submit("UserPromptSubmit", prompt, path)
            self.append_rollout(path, self.native_user("msg-first", prompt),
                                self.native_user("msg-early", "Synthetic early addition"))
            self.submit("UserPromptSubmit", "Synthetic early addition", path)
            self.append_rollout(
                path, padding, padding, padding, self.native_user("msg-middle", "Synthetic middle addition"),
                padding, padding, padding, padding, self.native_user("msg-late", "Synthetic late addition"),
            )
            # A successful bounded Prompt pass must leave its unread middle
            # available to later passes, even when the final page is visible.
            self.assertEqual(self.submit("UserPromptSubmit", "Synthetic late addition", path)["same_turn_capture"],
                             "bounded-pending")
            self.assertEqual(self.submit("Stop", "Synthetic last answer", path)["same_turn_capture"],
                             "bounded-pending")
            self.queue = self.f.restart()
            for _ in range(12):
                if not self.resume_codex_completions():
                    break
                self.queue = self.f.restart()
            else:
                self.fail("Contiguous completion did not settle")
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic last answer"), ("user", "Synthetic early addition"),
            ("user", "Synthetic late addition"), ("user", "Synthetic middle addition"), ("user", prompt),
        ])
        original = self.queue._queued()
        self.assertFalse(self.resume_codex_completions())
        self.assertEqual(self.queue._queued(), original)

    def test_codex_complete_scan_with_unanswered_form_releases_after_public_answer(self):
        prompt = "Synthetic prompt"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        form_call, reply = self.form("Synthetic late public answer")
        acknowledgement = ("response_item", {"type": "function_call_output", "call_id": "call-synthetic",
                                             "output": '{"accepted":true}'})
        self.append_rollout(path, self.native_user("msg-first", prompt), form_call, acknowledgement)
        final = "Synthetic response"
        result = self.submit("Stop", final, path)
        self.assertEqual(result["same_turn_capture"], "form-privacy-unverified")
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        self.assertTrue(delivery._has_work(self.queue.cache))
        self.queue = self.f.restart()
        self.assertTrue(self.resume_codex_completions())
        self.append_rollout(path, reply, ("event_msg", {
            "type": "task_started", "turn_id": "newer-turn", "root_turn_id": "newer-turn",
        }))
        self.assertFalse(self.resume_codex_completions(allowed=lambda scope: False))
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        self.f.cloud.request = accepted
        client = SimpleNamespace(actor="synthetic-owner", queue_identity={"synthetic": True},
                                 status=lambda session: {"source_admitted": True})
        core = SimpleNamespace(
            load=lambda home: self.value, approved=lambda value: True,
            legacy=lambda value, local: None,
            build_delivery_queue=lambda value, local, host: (client, self.queue),
            submit_hook=lambda value, local, payload: collection.submit_hook(
                value, local, payload, services=self.services,
            ),
        )
        task = {"schema": 1, "harness": "codex", "origin": self.local}
        with patch("agent_brain_client.adapters.storage.project_settings.source_active", return_value=True):
            self.assertTrue(delivery.deliver_origin(self.home, task, core))
            self.assertFalse(delivery.deliver_origin(self.home, task, core))
        self.assertEqual(self.queued_messages(), [
            ("assistant", final), ("user", "Synthetic late public answer"), ("user", prompt),
        ])
        self.assertFalse(list(self.queue.cache.glob("codex-completions/*.json")))
        self.assertEqual(self.queue.queue_status()["confirmed"], 3)

    def test_codex_complete_scan_with_unanswered_form_never_releases_private_answer(self):
        prompt = "Synthetic prompt"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        form_call, reply = self.form("LocalOnly: Synthetic late private answer")
        self.append_rollout(path, self.native_user("msg-first", prompt), form_call)
        final = "Synthetic derived answer"
        self.assertEqual(self.submit("Stop", final, path)["same_turn_capture"], "form-privacy-unverified")
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        unrelated = self.payload("Stop", "Synthetic independent final", "other-turn")
        result = collection.submit_hook(self.value, self.local, unrelated, services=self.services)
        self.assertEqual(result["status"], "staged")
        self.queue = self.f.restart()
        self.append_rollout(path, reply)
        self.assertFalse(self.resume_codex_completions())
        self.assertFalse(list(self.queue.cache.glob("codex-completions/*.json")))
        self.assertEqual(self.submit("Stop", final, path)["status"], "excluded-turn")
        self.assertEqual(self.queued_messages(), [("assistant", "Synthetic independent final"), ("user", prompt)])

    def test_codex_bounded_middle_private_form_never_releases_deferred_final(self):
        prompt = "Synthetic prompt"
        path = self.rollout()
        padding = ("response_item", {"type": "function_call_output", "output": "x" * 1400})
        self.submit("UserPromptSubmit", prompt, path)
        self.append_rollout(
            path, self.native_user("msg-first", prompt), padding, padding, padding,
            *self.form("LocalOnly: Synthetic form in the middle"), padding, padding, padding,
        )
        with patch.object(transcript, "_TAIL_LIMIT", 4096):
            self.assertEqual(self.submit("Stop", "Synthetic derived answer", path)["same_turn_capture"],
                             "bounded-pending")
            self.assertEqual(self.queued_messages(), [("user", prompt)])
            self.assertFalse(self.resume_codex_completions())
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        self.assertFalse(self.f.cloud.calls)

    def test_codex_seen_form_fences_final_on_rollout_failure(self):
        prompt = "Synthetic prompt"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        form_call, reply = self.form("LocalOnly: Synthetic private form")
        self.append_rollout(path, self.native_user("msg-first", prompt), form_call)
        self.submit("UserPromptSubmit", prompt, path)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("{unreadable native row}\n")
        self.append_rollout(path, reply)
        self.queue = self.f.restart()
        with patch.object(transcript, "snapshot", side_effect=OSError):
            result = self.submit("Stop", "Synthetic response using private answer", path)
        self.assertEqual(result["same_turn_capture"], "form-privacy-unverified")
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        self.assertEqual(self.submit("Stop", "Synthetic response using private answer", path)["status"],
                         "excluded-local-only-or-sensitive")
        self.assertEqual(self.queued_messages(), [("user", prompt)])

    def test_codex_prompt_native_observation_recovers_without_stop_after_enqueue_failure(self):
        prompt, addition = "Synthetic prompt", "Synthetic interrupted steer"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        self.append_rollout(path, self.native_user("msg-first", prompt), self.native_user("msg-steer", addition))
        with patch.object(self.queue, "_enqueue", side_effect=OSError):
            result = self.submit("UserPromptSubmit", addition, path)
        self.assertEqual(result["same_turn_capture_error_type"], "OSError")
        self.assertEqual(self.queued_messages(), [("user", prompt)])
        self.queue = self.f.restart()
        self.assertFalse(resume_staging(self.queue))
        original = self.queue._queued()
        self.assertEqual(self.queued_messages(), [("user", addition), ("user", prompt)])
        self.assertEqual(self.submit("UserPromptSubmit", addition, path)["status"], "staged")
        self.assertEqual(self.queue._queued(), original)

    def test_codex_supplement_interruption_retains_final_and_recovers_without_another_hook(self):
        prompt, addition = "Synthetic prompt", "Synthetic steer"
        path = self.rollout()
        self.submit("UserPromptSubmit", prompt, path)
        self.append_rollout(path, self.native_user("msg-first", prompt), self.native_user("msg-steer", addition))
        enqueue = self.queue._enqueue

        def interrupted(request):
            if request["params"]["event"]["messages"] == [{"role": "user", "content": addition}]:
                raise OSError("Synthetic append interruption")
            return enqueue(request)

        with patch.object(self.queue, "_enqueue", side_effect=interrupted):
            result = self.submit("Stop", "Synthetic final", path)
        self.assertEqual(result["same_turn_capture_error_type"], "OSError")
        self.assertEqual(self.queued_messages(), [("assistant", "Synthetic final"), ("user", prompt)])
        self.queue = self.f.restart()
        self.assertFalse(resume_staging(self.queue))
        self.assertEqual(self.queued_messages(),
                         [("assistant", "Synthetic final"), ("user", prompt), ("user", addition)])

    def test_codex_rollout_scope_failures_leave_direct_final_capture_available(self):
        prompt = "Synthetic initial prompt"
        first = self.native_user("msg-first", prompt)
        second = self.native_user("msg-second", "Synthetic unrelated text")
        for change in ({"turn": "older-turn"}, {"cwd": str(self.home / "other")},
                       {"session": "other-session"}):
            with self.subTest(change=change):
                path = self.rollout(first, second, **change)
                self.submit("UserPromptSubmit", prompt, path)
                result = self.submit("Stop", "Synthetic final", path)
                self.assertEqual(result["same_turn_capture"], "unavailable")
        path = self.rollout(first, second)
        stop_without_observed_prompt = dict(self.payload("Stop", "Synthetic other final", "older-turn"),
                                            transcript_path=path)
        collection.submit_hook(self.value, self.local, stop_without_observed_prompt, services=self.services)
        self.assertEqual(self.queued_messages(), [
            ("assistant", "Synthetic final"), ("assistant", "Synthetic other final"), ("user", prompt),
        ])

    def test_codex_and_project_long_prompt_and_final_use_shared_splitter(self):
        text = '測試😀' * 5000
        policy = {'schema': 1, 'enabled': True, 'scopes': [{'cwd': str(self.home),
                  'project_id': 'synthetic-project', 'session_id': 'synthetic-session'}]}
        p = {'harness': 'codex', 'device_home': str(self.home), 'cache': str(self.queue.cache),
             'codex_home': str(self.home / '.codex'), 'profile': 'synthetic',
             'profile_sha256': 'a' * 64, 'content_policy_sha256': 'b' * 64,
             'capture_policy': 'synthetic', 'capture_policy_sha256': 'c' * 64, 'content_policy': 'synthetic'}
        p['off_file'] = str(self.home / 'memory.off')
        for kind in ('UserPromptSubmit', 'Stop'):
            result = collection.submit_hook(self.value, self.local, self.payload(kind, text), services=self.services)
            self.assertEqual(self.captured(result['request_ids']), text)
            with patch('agent_brain_client.adapters.storage.pinned.pinned', return_value=policy), \
                 patch.object(event_policy, 'validate_hook_policy', return_value=policy), \
                 patch.object(event_policy, 'hook_enabled', return_value=True), \
                 patch('agent_brain_client.adapters.server.https.HTTPSContentClient'), \
                 patch('agent_brain_client.domain.content_policy.validate_policy'), \
                 patch('agent_brain_client.adapters.storage.content_session.ContentSession', return_value=self.queue):
                result = project_memory.capture(p, self.payload(kind, text, 'project-turn'))
            self.assertEqual(self.captured(result['request_ids']), text)
        self.assertFalse(self.f.cloud.calls)

    def test_long_local_only_excludes_entire_turn_and_never_opens_queue(self):
        for index, text in enumerate(('LocalOnly: ' + 'x' * 9000, 'x' * 9000 + '\nLocalOnly: private')):
            with self.subTest(text=text[:20]), patch.object(self.services, 'adapter') as opened:
                result = collection.submit_hook(self.value, self.local,
                    self.payload('UserPromptSubmit', text, 'turn-' + str(index)), services=self.services)
                self.assertEqual(result['status'], 'excluded-local-only-or-sensitive')
                result = collection.submit_hook(self.value, self.local,
                    self.payload('Stop', 'Synthetic answer derived from private prompt', 'turn-' + str(index)), services=self.services)
                self.assertEqual(result['status'], 'excluded-turn')
                opened.assert_not_called()
        self.assertEqual(self.queue._queued(), [])
        self.assertFalse(list(self.queue.cache.glob('observed-staging/*.json')))

    def test_confirmed_receipts_do_not_consume_active_capacity_or_disappear(self):
        self.stage()
        original = self.queue._queued()[0]
        for index in range(2049):
            row = copy.deepcopy(original)
            request = row['request']
            request['request_id'] = 'synthetic_receipt_' + str(index).zfill(8)
            row.update(request_sha=metadata.digest(contract.encode(request)),
                       state='confirmed', response=accepted(request))
            self.queue._save('outbox/' + request['request_id'] + '.json', row)
        result = self.stage(operation='new-turn')
        self.assertTrue(result['local_queue']['saved'])
        self.assertEqual(self.queue.queue_status()['confirmed'], 2049)
        self.assertEqual(self.queue.queue_status()['pending'], 2)
        self.assertEqual(len(self.queue._queued()), 2051)
        from agent_brain_client.adapters.installation.lifecycle import queue_counts
        counts = queue_counts(self.queue)
        self.assertEqual(counts['confirmed'], 2049)
        self.assertEqual(counts['pending'], 2)

    def test_project_long_private_or_restricted_prompt_excludes_following_assistant(self):
        policy = {'schema': 2, 'scopes': [{'cwd': str(self.home),
                  'project_id': 'synthetic-project', 'session_id': 'synthetic-session'}]}
        p = {'profile_sha256': 'a' * 64, 'content_policy_sha256': 'b' * 64,
             'capture_policy': 'synthetic', 'capture_policy_sha256': 'c' * 64,
             'off_file': str(self.home / 'memory.off')}
        for index, prefix in enumerate(('LocalOnly: ', 'do not share this: ') * 2):
            policy['schema'] = 1 if index < 2 else 2
            with patch('agent_brain_client.adapters.storage.pinned.pinned', return_value=policy), \
                 patch.object(event_policy, 'validate_hook_policy', return_value=policy), \
                 patch.object(event_policy, 'hook_enabled', return_value=True), \
                 patch('agent_brain_client.adapters.server.https.HTTPSContentClient') as client:
                for kind, text in [('UserPromptSubmit', prefix + 'x' * 9000),
                                   ('Stop', 'Synthetic derived assistant reply')]:
                    self.assertEqual(project_memory.capture(p, self.payload(kind, text, 'project-private-' + str(index)))['status'], 'excluded-turn')
                client.assert_not_called()

    def test_every_multibatch_interruption_recovers_exact_tail_without_hook_replay(self):
        text = 'Synthetic prompt ' * 8000 + 'SYNTHETIC_FINAL_TAIL'
        for failure in (1, 2, 3):
            with self.subTest(failure=failure):
                enqueue = self.queue._enqueue
                count = 0
                def interrupted(request):
                    nonlocal count
                    count += 1
                    if count == failure:
                        raise OSError('Synthetic process interruption')
                    return enqueue(request)
                with patch.object(self.queue, '_enqueue', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.stage(text=text, operation='turn-' + str(failure))
                journal = next(self.queue.cache.glob('observed-staging/*.json'))
                requests = self.queue._load('observed-staging/' + journal.name)['requests']
                self.queue = self.f.restart()
                self.f.cloud.request = accepted
                self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
                ids = [r['request_id'] for r in requests]
                self.assertEqual(self.captured(ids), text)
                for request in requests:
                    self.assertEqual(self.queue._load('outbox/' + request['request_id'] + '.json')['request'], request)
                self.assertFalse(list(self.queue.cache.glob('observed-staging/*.json')))
                self.assertFalse(resume_staging(self.queue))

    def test_full_active_queue_retains_whole_observation_until_capacity_returns(self):
        # Inject capacity refusal before the first row, then let normal delivery
        # recover the observation from disk without another native event.
        text = 'Synthetic long message ' * 9000
        with patch.object(self.queue, '_enqueue', side_effect=CacheBlocked):
            with self.assertRaises(CacheBlocked):
                self.stage(text=text)
        journal = next(self.queue.cache.glob('observed-staging/*.json'))
        requests = self.queue._load('observed-staging/' + journal.name)['requests']
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.captured([r['request_id'] for r in requests]), text)

    def test_real_active_budget_stays_bounded_and_resumes_after_confirmation(self):
        self.stage()
        original = self.queue._queued()[0]
        for index in range(1023):
            row = copy.deepcopy(original)
            request = row['request']
            request['request_id'] = 'synthetic_pending_' + str(index).zfill(8)
            row['request_sha'] = metadata.digest(contract.encode(request))
            self.queue._save('outbox/' + request['request_id'] + '.json', row)
        with self.assertRaises(CacheBlocked):
            self.stage(operation='blocked-by-active-budget')
        self.assertEqual(self.queue.queue_status()['pending'], 1024)
        journal = next(self.queue.cache.glob('observed-staging/*.json'))
        request = self.queue._load('observed-staging/' + journal.name)['requests'][0]
        original.update(state='confirmed', response=accepted(original['request']))
        self.queue._save('outbox/' + original['request']['request_id'] + '.json', original)
        self.assertFalse(resume_staging(self.queue))
        self.assertEqual(self.queue.queue_status()['pending'], 1024)
        self.assertEqual(self.queue._load('outbox/' + request['request_id'] + '.json')['request'], request)

    def test_idle_supervisor_detects_durable_rows_after_hook_dies_before_schedule(self):
        for harness, base in [('codex', 'global-memory'), ('codebuddy', 'codebuddy')]:
            with self.subTest(harness=harness):
                cache = self.home / '.config/agent-brain-client' / base / 'outboxes' / digest(self.local)
                queue = ContentSession(self.home, cache, fixture.POLICY, self.f.cloud,
                                           host=CodexContentPorts, native_home=self.home / '.codex')
                metadata.write(cache / 'origin.json', self.local, cache=True)
                with patch.object(delivery, 'wake'):
                    delivery.schedule_delivery(self.home, harness, self.local)
                with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                     patch.object(delivery, 'deliver_origin', return_value=False):
                    delivery.run(self.home)
                stage_messages(queue, project_id='synthetic-project', session_id='synthetic-session',
                    operation_id='new-' + harness, phase='prompt', messages=[{'role': 'user', 'content': 'Synthetic'}])
                delivery.discover_existing(self.home)
                with patch.object(delivery.subprocess, 'Popen') as spawn:
                    self.assertTrue(delivery.wake(self.home))
                    spawn.assert_called_once()

    def test_project_route_is_persisted_before_capture_and_rediscovered(self):
        from agent_brain_client.adapters.storage import project_settings as project_configuration
        p = {'harness': 'codex', 'device_home': str(self.home), 'project_root': str(self.home),
             'cache': str(self.queue.cache), 'capture_policy': 'synthetic', 'source_mode': 'codex-project'}
        with patch.object(project_configuration, 'load', return_value=p), patch.object(delivery, 'wake') as wake:
            delivery.schedule_project(self.home, self.home / 'settings.json', 'a' * 64, wake_worker=False)
            wake.assert_not_called()
        task = next(delivery.directory(self.home).glob('*.json'))
        old_time = task.stat().st_mtime_ns
        self.stage()
        with patch.object(project_configuration, 'load', return_value=p):
            delivery.discover_existing(self.home)
        self.assertGreaterEqual(task.stat().st_mtime_ns, old_time)

    def test_splitter_does_not_turn_inline_privacy_example_into_directive(self):
        for marker in ('LocalOnly:', '[private]', 'do not share this:'):
            text = 'x' * 4096 + marker + ' synthetic inline example'
            result = self.stage(text=text, operation='inline-' + digest(marker))
            self.assertEqual(self.captured(result['request_ids']), text)
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.queue.queue_status()['pending'], 0)

    def test_invalid_journal_is_retained_for_review_without_starving_valid_pending(self):
        for index, value in enumerate((None, [], {'schema': 1, 'requests': [], 'requests_sha256': 'changed'})):
            self.queue._save('observed-staging/' + str(index) * 64 + '.json', value)
        malformed = self.queue.cache / 'observed-staging' / ('a' * 64 + '.json')
        malformed.write_bytes(b'{broken synthetic json')
        malformed.chmod(0o600)
        self.stage()
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.queue.queue_status()['confirmed'], 1)
        reviewed = list(self.queue.cache.glob('observed-review/*.json'))
        self.assertEqual(len(reviewed), 4)
        self.assertIn(b'{broken synthetic json', [path.read_bytes() for path in reviewed])

    def test_project_registration_failure_still_recalls_current_question(self):
        import sys
        import io
        p = {'harness': 'codex', 'device_home': str(self.home), 'project_root': str(self.home),
             'capture_policy': 'synthetic', 'source_mode': 'codex-project'}
        stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(self.payload('UserPromptSubmit', 'Synthetic question')).encode()))
        with patch.object(sys, 'stdin', stream), \
             patch.object(project_memory, 'active', return_value=True), \
             patch.object(delivery, 'schedule_project', side_effect=OSError), \
             patch.object(project_memory, 'recalled', return_value=[{'content': 'Synthetic recall'}]) as recall, \
             patch.object(project_memory, 'context', return_value='Synthetic recall'):
            result = project_memory.hook_payload(p, settings_path=self.home / 'settings.json', settings_sha256='a' * 64)
        recall.assert_called_once()
        self.assertIn('additionalContext', result['hookSpecificOutput'])


if __name__ == '__main__':
    unittest.main()
