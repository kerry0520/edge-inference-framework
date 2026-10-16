"""Native budget rejection evidence is opt-in and must preserve history."""
from copy import deepcopy
import unittest

from test_phone_verify_session import fixture, v


def with_rejection(at=1.5):
    s, stdout, cli = fixture()
    previous = s['requests'][0 if at < 2 else 1]
    messages = deepcopy(previous['request']['messages']) + [
        {'role': 'assistant', 'content': previous['response']['text']},
        {'role': 'user', 'content': 'too much input'}]
    q = {'id': 'budget', 'session_token': s['session_id'], 'messages': messages,
         'max_new_tokens': 8, 'max_context_tokens': 512}
    r = {'id': 'budget', 'pid': 7, 'load_count': 1, 'event': 'rejected',
         'reason': 'context_budget_exceeded', 'input_tokens': 600,
         'max_new_tokens': 8, 'max_context_tokens': 512}
    s['rejected_requests'] = [{'request': q, 'response': r, 'state': 'rejected',
                               'submitted_at_unix': at, 'sent_at_unix': at + .01,
                               'frames': [{'message': deepcopy(r), 'elapsed_seconds': .2}]}]
    return s, stdout, cli


class BudgetReviewTests(unittest.TestCase):
    def test_native_rejection_requires_explicit_opt_in(self):
        data = with_rejection()
        with self.assertRaisesRegex(ValueError, 'opt-in'):
            v.verify(*data)
        result = v.verify(*data, allow_budget_rejections=True)
        self.assertEqual(result['confirmed_budget_rejections'], 1)
        self.assertEqual(result['successful_requests'], 2)
        self.assertEqual(result['actual_forwards'], 6)

    def test_rejection_after_last_success_does_not_change_history(self):
        result = v.verify(*with_rejection(4), allow_budget_rejections=True)
        self.assertTrue(result['accepted'])

    def test_invalid_native_rejection_frames_fail(self):
        changes = [lambda t: t['response'].update(pid=8),
                   lambda t: t['response'].update(input_tokens=1),
                   lambda t: t['response'].update(input_tokens=True),
                   lambda t: t['response'].update(max_context_tokens=513),
                   lambda t: t['response'].update(reason='other'),
                   lambda t: t.update(state='unknown'),
                   lambda t: t.update(frames=[]),
                   lambda t: t['frames'].append(deepcopy(t['frames'][0])),
                   lambda t: t.update(partial_bytes_hex='61')]
        for change in changes:
            with self.subTest(change=change):
                s, out, cli = with_rejection()
                change(s['rejected_requests'][0])
                with self.assertRaises((ValueError, KeyError)):
                    v.verify(s, out, cli, allow_budget_rejections=True)

    def test_rejected_history_id_and_timing_must_be_consistent(self):
        changes = [lambda t: t['request']['messages'][1].update(content='wrong history'),
                   lambda t: t['request'].update(session_token='wrong'),
                   lambda t: t['request'].update(id='0'),
                   lambda t: t.update(submitted_at_unix=1.05),
                   lambda t: t.update(sent_at_unix=4)]
        for change in changes:
            with self.subTest(change=change):
                s, out, cli = with_rejection()
                change(s['rejected_requests'][0])
                with self.assertRaises(ValueError):
                    v.verify(s, out, cli, allow_budget_rejections=True)

    def test_no_rejection_default_and_opt_in_agree(self):
        data = fixture()
        self.assertEqual(v.verify(*data), v.verify(*data, allow_budget_rejections=True))


if __name__ == '__main__':
    unittest.main()
