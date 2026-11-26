import json
import tempfile
import unittest
from pathlib import Path

from usagecourt import store


class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / 'events.sqlite'
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.apply('meter', 'meter_normal')

    def apply(self, subsystem, fixture):
        file = Path(__file__).resolve().parents[1] / 'fixtures' / (fixture + '.json')
        return [store.run(self.db, subsystem, event) for event in json.loads(file.read_text(encoding='utf-8'))]

    def correct(self):
        return store.run(self.db, 'meter', dict(op='correct', event_id='late-units', id='sample-a2',
                                              old='sample-a', units=3, source='node-correction'))

    def test_carbon_declaration_holds_original_quantity_when_evidence_reopens(self):
        results = self.apply('carbon', 'carbon_examples')
        self.assertEqual(results[1], {'decision': 'declared', 'grams': 120, 'factor': 'east-baseline'})
        self.assertEqual(self.correct()['decision'], 'evidence_corrected')
        result = store.run(self.db, 'carbon', dict(op='inspect', event_id='late-inspect', tenant='tenant-a'))
        self.assertEqual((result['declarations'][0]['grams'], result['declarations'][0]['effective']), (120, 'review'))
        self.assertTrue(store.replay(self.path)['verified'])

    def test_quota_has_no_duplicate_charge_and_flags_review(self):
        results = self.apply('quota', 'quota_examples')
        self.assertEqual(results[1], {'decision': 'quota_charged', 'available': 10})
        duplicate = store.run(self.db, 'quota', dict(op='charge', event_id='quota-repeat',
                      pool='tenant-base-quota', tenant='tenant-a', case='meter-a'))
        self.assertEqual(duplicate['decision'], 'case_already_charged')
        self.assertEqual(self.correct()['decision'], 'evidence_corrected')
        result = store.run(self.db, 'quota', dict(op='assess', event_id='late-assess',
                                                 pool='tenant-base-quota', tenant='tenant-a'))
        self.assertEqual(result['pending_cases'], ['meter-a'])
        self.assertTrue(store.replay(self.path)['verified'])
