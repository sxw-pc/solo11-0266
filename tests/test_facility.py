import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from usagecourt.store import connect, replay, run

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_new_reading_changes_three_independent_facility_decisions(self):
        manifest = json.loads((ROOT / 'fixtures/deployment_examples.json').read_text())
        with TemporaryDirectory() as location:
            path = str(Path(location) / 'facility.sqlite')
            with connect(path) as db:
                results = []
                for step in manifest:
                    commands = json.loads((ROOT / step['fixture']).read_text())
                    results.append([run(db, step['subsystem'], c)['decision'] for c in commands])
                self.assertEqual(results[1][-3:], ['energize_allowed', 'service_in_place', 'claim_threshold_not_met'])
                self.assertEqual(results[3], ['power_insufficient', 'remove_for_service', 'claim_eligible'])
            db.close()
            self.assertEqual(replay(path), {'verified': True, 'decisions': 10, 'divergences': []})

    def test_invalid_observation_cannot_replace_a_valid_reading(self):
        db = connect(':memory:')
        self.addCleanup(db.close)
        reading = dict(event_id='a1', op='record', serial='A', running_watts=149, clearance_mm=500, shock_g=0)
        self.assertEqual(run(db, 'appliance', reading)['decision'], 'appliance_observed')
        self.assertEqual(run(db, 'appliance', dict(reading, event_id='a2', running_watts=False))['decision'], 'invalid_appliance_observation')
        run(db, 'facility', dict(event_id='b1', op='register_bay', bay='R', available_watts=100, service_aisle_mm=500))
        self.assertEqual(run(db, 'facility', dict(event_id='b2', op='energize', bay='R', serial='A'))['decision'], 'power_insufficient')
