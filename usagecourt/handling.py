"""Evaluate a lift, a cable fit or the receiving wash line before moving equipment."""
from .sled import current
from .sealed_intake import OPERATIONS as SEALED_OPERATIONS, WASH_LIMIT_MG
from .sealed_intake import handle as sealed_handle


def handle(db, command):
    op = command['op']
    if op in SEALED_OPERATIONS:
        return sealed_handle(db, command)
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
    if op == 'receive' and command.get('frame'):
        # A frame's two probe columns are not per-item serials.  Signing one
        # item off from a raw column reading is exactly the favourable-value
        # shortcut the sealed intake must not allow.
        return {'decision': 'frame_direction_unresolved',
                'claimable': [], 'reason': 'use_sealed_direction_evidence'}
    sled = current(db, command['serial'])
    if sled is None:
        return {'decision': 'sled_unmeasured'}
    if op == 'fit':
        fits = sled['connector'] in command['accepted_connectors']
        return {'decision': 'cable_fits' if fits else 'different_harness_required'}
    # The bench's real single-item standard is bounded by WASH_LIMIT_MG even
    # when the caller passes a looser entry limit.
    limit = min(command['max_residue_mg'], WASH_LIMIT_MG)
    clean = sled['residue_mg'] <= limit
    return {'decision': 'receive_direct' if clean else 'wash_before_receiving'}
