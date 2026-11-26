import json
import tempfile
import unittest
from pathlib import Path

from usagecourt import store


class PlatformSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.sqlite'
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.apply('meter', 'meter_normal')

    def apply(self, subsystem, fixture):
        source = Path(__file__).resolve().parents[1] / 'fixtures' / (fixture + '.json')
        return [store.run(self.db, subsystem, event) for event in json.loads(source.read_text(encoding='utf-8'))]

    def test_license_seat_closes_for_the_original_uid(self):
        records = self.apply('license', 'license_examples')
        self.assertEqual(records[1]['remaining'], 0)
        self.assertEqual(records[2]['decision'], 'no_seat')
        self.assertEqual(records[3]['sessions'][0]['uid'], 'uid-a')
        self.assertEqual(records[-1]['remaining'], 1)
        self.assertTrue(store.replay(self.path)['verified'])

    def test_collection_check_distinguishes_gap_from_end_of_stream(self):
        records = self.apply('inspection', 'inspection_examples')
        self.assertEqual(records[3], {'decision':'collection_incomplete', 'gaps':[[1,2]]})
        self.assertEqual(records[-1], {'decision':'collection_complete','gaps':[]})
        self.assertTrue(store.replay(self.path)['verified'])
