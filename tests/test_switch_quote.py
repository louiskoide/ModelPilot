import unittest
from modelpilot import switch_quote as sq

S, O = ('claude-sonnet-5-5', 'low'), ('claude-opus-5-5', 'high')


class SwitchQuote(unittest.TestCase):
    def test_small_context_switches(self):
        q = sq.quote(S, O, 10000)
        self.assertEqual(q['action'], 'switch')
        self.assertFalse(q['applied'])

    def test_large_context_delegates(self):
        self.assertEqual(sq.quote(S, O, 120000)['action'], 'delegate')

    def test_oversized_helper_warns(self):
        q = sq.quote(S, O, 120000, {'reread_tokens': 50000})
        self.assertEqual(q['action'], 'warn')

    def test_same_setting_is_free(self):
        self.assertEqual(sq.quote(S, S, 120000)['rewrite_usd'], 0.0)

    def test_ledger_quote_and_notice(self):
        import os
        import tempfile
        from modelpilot.governor import Governor
        from modelpilot import hooks
        with tempfile.TemporaryDirectory() as d:
            gov = Governor(os.path.join(d, 'g.db'), 's', 10.0)
            self.assertIsNone(sq.from_ledger(gov, [{'kind': 'model', 'value': S[0]}]))
            gov.observe_context(S[0], 120000, {'output': 15, 'write': 3.75, 'read': 0.3}, '5m')
            changes = [{'kind': 'model', 'value': O[0]}]
            q = sq.from_ledger(gov, changes, 'low')
            self.assertEqual(q['action'], 'delegate')
            plan = {'trigger': 'idle', 'plan_id': 'p', 'changes': [dict(c, change_id='c', revision=1) for c in changes]}
            self.assertIn('Estimated switch cost', hooks.rebase_notice(plan, q))
            gov.close()


if __name__ == '__main__':
    unittest.main()
