import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from usagecourt import delivery
from usagecourt.store import connect, one, replay, rows, run

ROOT = Path(__file__).resolve().parents[1]
INSTALL = json.loads((ROOT / 'fixtures/sram_install.json').read_text(encoding='utf-8'))[0]
ORDERS = json.loads((ROOT / 'fixtures/sram_orders.json').read_text(encoding='utf-8'))


class SramDeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'delivery.sqlite')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.assertEqual(run(self.db, 'die', INSTALL)['decision'], 'installed')
        self.number = 0

    def fixture(self, name):
        return [run(self.db, x.get('subsystem', 'delivery'), x)
                for x in json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))]

    def event(self, subsystem='delivery', **data):
        self.number += 1
        return run(self.db, subsystem, dict(event_id=f'sram-{self.number}', die='D17', **data))

    def burn(self, rows, columns):
        return self.event('die', op='burn', image={'rows': rows, 'columns': columns})

    def deliver(self, contract, rows, reserve, pattern='shared', **extra):
        return self.event(op='deliver', id=f'delivery-{contract}', contract=contract,
                          rows=rows, reserve_spare_columns=reserve, pattern=pattern, **extra)

    def access(self, delivery_id, cell, mode='read'):
        return self.event(op='access', delivery=delivery_id, mode=mode, cell=cell)

    def plan(self, contract, rows, reserve, pattern='shared'):
        return self.event(op='plan', contract=contract, rows=rows,
                          reserve_spare_columns=reserve, pattern=pattern)

    def test_two_contract_fixtures_run_in_separate_empty_databases(self):
        for fixture, cells, spares in [('sram_full_order.json', 9, 0),
                                       ('sram_reserve_order.json', 6, 1)]:
            with TemporaryDirectory() as tmp:
                db = connect(str(Path(tmp) / 'order.sqlite'))
                results = [run(db, x.get('subsystem', 'delivery'), x)
                           for x in json.loads((ROOT / 'fixtures' / fixture).read_text(encoding='utf-8'))]
                delivered = next(r for r in results if r['decision'] == 'delivered')
                self.assertEqual(delivered['evidence']['cells'], cells)
                self.assertEqual(delivered['unused_spare_columns'], spares)
                self.assertEqual(delivered['evidence']['shared'], 'pattern_passed')
                self.assertEqual(replay(str(Path(tmp) / 'order.sqlite'))['verified'], True)
                db.close()

    def test_full_contract_delivers_nine_addresses_and_rejects_outside(self):
        result = self.deliver('full', ORDERS['full']['rows'], 0, ORDERS['pattern'])
        self.assertEqual(result['decision'], 'delivered')
        self.assertEqual(result['evidence']['cells'], 9)
        self.assertEqual(result['image'], {'rows': {'2': 3}, 'columns': {'0': 3, '1': 4}})
        self.assertEqual([self.access(result['delivery'], [r, c])['decision']
                          for r in (0, 1, 2) for c in (0, 1, 2)],
                         ['address_allowed'] * 9)
        self.assertEqual(self.access(result['delivery'], [0, 3])['decision'], 'address_out_of_scope')
        self.assertEqual(self.access(result['delivery'], [3, 0])['decision'], 'address_out_of_scope')
        receipt = one(self.db, "SELECT * FROM deliveries WHERE id='delivery-full'")
        self.assertEqual(receipt['status'], 'delivered')
        self.assertEqual(json.loads(receipt['rows']), [0, 1, 2])
        self.assertEqual(json.loads(receipt['image_before']), {'rows': {}, 'columns': {}})
        self.assertEqual(json.loads(receipt['image']), {'rows': {'2': 3}, 'columns': {'0': 3, '1': 4}})
        stored = json.loads(one(self.db, "SELECT output FROM die_runs WHERE id=?",
                                (receipt['test_run'],))['output'])
        self.assertEqual(stored['decision'], 'pattern_passed')
        self.assertEqual(json.loads(receipt['physical'])['2,1'], [3, 4])

    def test_reserve_contract_delivers_six_plus_one_spare_column(self):
        result = self.deliver('reserve', ORDERS['reserve']['rows'], 1, ORDERS['pattern'])
        self.assertEqual(result['decision'], 'delivered')
        self.assertEqual(result['evidence']['cells'], 6)
        self.assertEqual(result['unused_spare_columns'], 1)
        self.assertEqual(set(result['image']['columns']), {'1'})
        self.assertEqual([self.access(result['delivery'], [r, c])['decision']
                          for r in (0, 1) for c in (0, 1, 2)], ['address_allowed'] * 6)
        self.assertEqual(self.access(result['delivery'], [2, 0])['decision'], 'address_out_of_scope')
        self.assertEqual(self.access(result['delivery'], [0, 4], 'write')['decision'], 'address_out_of_scope')
        self.assertEqual(self.access(result['delivery'], [0, 0], 'write')['decision'], 'address_allowed')

    def test_isolated_pass_with_shared_failure_is_not_certified(self):
        static_image = {'rows': {'0': 3}, 'columns': {'1': 3, '2': 4}}
        self.assertEqual(self.burn(static_image['rows'], static_image['columns'])['decision'], 'burned')
        self.assertEqual(self.event('patterns', op='test', pattern='isolated', rows=[0, 1, 2])
                         ['decision'], 'pattern_passed')
        failed = self.event('patterns', op='test', pattern='shared', rows=[0, 1, 2])
        self.assertEqual(failed['failures'], [{'kind': 'coupled_read', 'aggressor': [2, 1],
                                               'cell': [1, 1], 'physical': [1, 3]}])
        result = self.deliver('full', [0, 1, 2], 0, image=static_image)
        self.assertEqual(result['decision'], 'delivery_test_failed')
        self.assertEqual(result['evidence']['isolated'], 'pattern_passed')
        self.assertEqual(result['evidence']['shared'], 'pattern_failed')
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM deliveries")['n'], 0)

    def test_repeated_burn_event_takes_effect_once(self):
        result = self.deliver('full', [0, 1, 2], 0)
        receipt = one(self.db, "SELECT * FROM deliveries WHERE id='delivery-full'")
        self.assertEqual((result['revision'], receipt['revision']), (1, 1))
        before = one(self.db, "SELECT revision,image FROM memory_dies WHERE id='D17'")
        again = self.deliver('full', [0, 1, 2], 0)
        self.assertEqual(again['decision'], 'already_delivered')
        after = one(self.db, "SELECT revision,image FROM memory_dies WHERE id='D17'")
        self.assertEqual((before['revision'], before['image']), (after['revision'], after['image']))
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM deliveries")['n'], 1)
        # The same event id replayed is also a single decision.
        command = {'event_id': 'repeat-delivery', 'op': 'deliver', 'die': 'D17',
                   'id': 'repeat-delivery', 'contract': 'full', 'rows': [0, 1, 2],
                   'reserve_spare_columns': 0, 'pattern': 'shared'}
        first = run(self.db, 'delivery', command)
        self.assertEqual(first['decision'], 'no_feasible_image')
        self.assertEqual(run(self.db, 'delivery', command), first)
        self.assertEqual(run(self.db, 'delivery', dict(command, rows=[0, 1]))['decision'],
                         'event_identity_conflict')

    def test_partial_burn_keeps_delivery_modes_separate(self):
        self.assertEqual(self.burn({'0': 3}, {})['decision'], 'burned')
        full = self.plan('full', [0, 1, 2], 0)
        self.assertEqual(full['decision'], 'no_feasible_image')
        self.assertEqual(full['occupied'], {'rows': {'0': 3}, 'columns': {}})
        self.assertEqual(full['best_failure']['failures'][0]['kind'], 'coupled_read')
        self.assertEqual(full['best_failure']['failures'][0]['aggressor'], [2, 1])
        reserve = self.plan('reserve', [0, 1], 1)
        self.assertEqual(reserve['decision'], 'plan_ready')
        self.assertEqual(reserve['unused_spare_columns'], 1)
        result = self.deliver('reserve', [0, 1], 1)
        self.assertEqual(result['decision'], 'delivered')
        self.assertEqual(result['image'], {'rows': {'0': 3}, 'columns': {'1': 3}})

    def test_occupied_target_cannot_move_or_swap(self):
        self.assertEqual(self.deliver('reserve', [0, 1], 2)['image'],
                         {'rows': {'0': 3}, 'columns': {'1': 3, '2': 4}})
        moved = self.deliver('reserve', [0, 1], 2, image={'rows': {'0': 3},
                                                          'columns': {'1': 3, '2': 3}})
        self.assertEqual(moved['decision'], 'fuse_cannot_change')
        rebound = self.deliver('reserve', [0, 1], 2, image={'rows': {'0': 3},
                                                            'columns': {'1': 4, '2': 3}})
        self.assertEqual(rebound['decision'], 'fuse_cannot_change')
        released = self.deliver('reserve', [0, 1], 1, image={'rows': {'0': 3}, 'columns': {}})
        self.assertEqual(released['decision'], 'fuse_cannot_change')

    def test_receipt_goes_stale_when_a_later_burn_moves_a_cell(self):
        result = self.deliver('reserve', [0, 1], 1)
        self.assertEqual(self.burn({'0': 3}, {'1': 3, '2': 4})['decision'], 'burned')
        self.assertEqual(self.access(result['delivery'], [0, 2])['decision'], 'delivery_image_changed')
        self.assertEqual(self.access(result['delivery'], [0, 1])['decision'], 'address_allowed')

    def test_delivery_rejects_incompatible_image_before_burning(self):
        result = self.deliver('reserve', [0, 1], 1,
                              image={'rows': {'1': 3}, 'columns': {'1': 3}})
        self.assertEqual(result['decision'], 'fuse_cannot_change')
        self.assertEqual(one(self.db, "SELECT revision FROM memory_dies WHERE id='D17'")['revision'], 0)

    def test_supplied_image_must_extend_the_actual_fuse_mirror(self):
        self.assertEqual(self.burn({'0': 3}, {})['decision'], 'burned')
        result = self.deliver('reserve', [0, 1], 1, image={'rows': {'0': 3},
                                                           'columns': {'1': 3}})
        self.assertEqual(result['decision'], 'delivered')
        self.assertEqual(one(self.db, "SELECT revision FROM memory_dies WHERE id='D17'")['revision'], 2)
        self.assertEqual(result['evidence']['cells'], 6)


class SramDeliveryCliTests(unittest.TestCase):
    def test_both_orders_cli_and_replay(self):
        import subprocess
        import sys
        for fixture, decision in [('fixtures/sram_full_order.json', 'delivered'),
                                  ('fixtures/sram_reserve_order.json', 'delivered')]:
            with TemporaryDirectory() as tmp:
                db = str(Path(tmp) / 'cli.sqlite')
                out = subprocess.run([sys.executable, '-B', '-m', 'usagecourt', 'delivery',
                                      '--db', db, '--fixture', fixture],
                                     cwd=ROOT, capture_output=True, text=True, check=True)
                result = json.loads(out.stdout)[0]['results']
                self.assertEqual(result[1]['decision'], decision)
                self.assertEqual(result[1]['evidence']['shared'], 'pattern_passed')
                replay_out = subprocess.run([sys.executable, '-B', '-m', 'usagecourt', 'replay',
                                             '--db', db], cwd=ROOT, capture_output=True, text=True, check=True)
                self.assertTrue(json.loads(replay_out.stdout)['verified'])


if __name__ == '__main__':
    unittest.main()
