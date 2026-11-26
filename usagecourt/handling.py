"""Evaluate a lift, a cable fit or the receiving wash line before moving equipment."""
from .sled import current


def handle(db, command):
    op = command['op']
    if op == 'lift':
        serials = command['sleds']
        if not serials or len(set(serials)) != len(serials):
            return {'decision': 'invalid_lift_manifest'}
        sleds = [current(db, serial) for serial in serials]
        if None in sleds:
            return {'decision': 'sled_unmeasured'}
        grams = sum(sled['grams'] for sled in sleds)
        moment = sum(sled['grams'] * sled['offset_mm'] for sled in sleds)
        permitted = grams <= command['max_grams'] and abs(moment) <= grams * command['max_offset_mm']
        return {'decision': 'lift_balanced' if permitted else 'lift_rejected',
                'grams': grams, 'moment_g_mm': moment}
    if op not in ('fit', 'receive'):
        return {'decision': 'unknown_operation'}
    sled = current(db, command['serial'])
    if sled is None:
        return {'decision': 'sled_unmeasured'}
    if op == 'fit':
        fits = sled['connector'] in command['accepted_connectors']
        return {'decision': 'cable_fits' if fits else 'different_harness_required'}
    clean = sled['residue_mg'] <= command['max_residue_mg']
    return {'decision': 'receive_direct' if clean else 'wash_before_receiving'}
