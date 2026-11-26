import json
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
import unittest

from usagecourt.store import connect, replay, run

ROOT = Path(__file__).resolve().parents[1]


class HandlingTests(unittest.TestCase):
    def test_same_trays_change_decisions_after_refitting(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'handling.sqlite')
            with closing(connect(path)) as db:
                outcomes = []
                manifest = json.loads((ROOT / 'fixtures/handling_examples.json').read_text())
                for step in manifest:
                    commands = json.loads((ROOT / step['fixture']).read_text())
                    outcomes.append([run(db, step['subsystem'], c)['decision'] for c in commands])
            self.assertEqual(outcomes[1], ['lift_balanced', 'cable_fits', 'receive_direct'])
            self.assertEqual(outcomes[3], ['lift_rejected', 'different_harness_required', 'wash_before_receiving'])
            self.assertEqual(replay(path), {'verified': True, 'decisions': 9, 'divergences': []})

    def test_lift_counts_each_tray_once_and_requires_measurements(self):
        with closing(connect(':memory:')) as db:
            base = dict(op='lift', max_grams=18000, max_offset_mm=10)
            self.assertEqual(run(db, 'handling', dict(base, sleds=['A','A'], event_id='e1')), {'decision': 'invalid_lift_manifest'})
            self.assertEqual(run(db, 'handling', dict(base, sleds=['A'], event_id='e2')), {'decision': 'sled_unmeasured'})
