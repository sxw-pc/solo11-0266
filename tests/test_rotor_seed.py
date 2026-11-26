import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from usagecourt.store import connect, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/rotor_install.json'


class RotorSeedTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / 'stand.sqlite')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        for row in json.loads(FIXTURE.read_text()):
            self.assertIn(run(self.db, 'rotor', row)['decision'], ('stocked','installed'))
        self.sequence = 0

    def apply(self, subsystem, **data):
        self.sequence += 1
        return run(self.db, subsystem, dict(event_id=f't-{self.sequence}', **data))

    def mount(self, head, a, b):
        return self.apply('rotor', op='mount', head=head, a=a, b=b)

    def spin(self, heads, phase='270'):
        return self.apply('spinstand', op='spin', heads=heads, phase=phase,
                          individual_limit=1, stand_limit=1)

    def test_independent_trials_pass_but_shared_stand_can_fail(self):
        self.mount('A',['w1'],['w2'])
        self.mount('B',['w3'],['w4'])
        for head in ('A','B'):
            self.assertEqual(self.spin([head])['decision'],'qualified')
        together = self.spin(['A','B'])
        self.assertEqual((together['decision'], together['revisions']), ('out_of_envelope',[1,1]))
        self.assertEqual(together['by_speed']['low']['base_squared'],4)
        self.mount('A',['w1','w5'],['w2'])
        repaired = self.spin(['A','B'])
        self.assertEqual((repaired['decision'], repaired['revisions']), ('qualified',[2,1]))
        self.assertEqual(repaired['by_speed']['low']['base_squared'],1)
        self.assertTrue(replay(self.path)['verified'])

    def test_same_tokens_can_be_transferred_after_removal(self):
        self.assertEqual(self.mount('A',['w1'],['w2'])['decision'],'mounted')
        self.assertEqual(self.mount('B',['w1'],['w2'])['decision'],'token_already_mounted')
        self.mount('A',[],[])
        self.assertEqual(self.mount('B',['w1'],['w2'])['decision'],'mounted')
        self.assertEqual(self.spin(['B'])['decision'],'qualified')
        self.assertTrue(replay(self.path)['verified'])
