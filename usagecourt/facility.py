"""Decide installation, service access and carrier claims from appliance observations."""
from .appliance import observed
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS facility_bays(
    id TEXT PRIMARY KEY, available_watts INTEGER NOT NULL,
    service_aisle_mm INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS freight_contracts(
    id TEXT PRIMARY KEY, claim_threshold_g INTEGER NOT NULL
);
"""
TABLES = ('facility_bays', 'freight_contracts')


def handle(db, command):
    action = command['op']
    if action == 'register_bay':
        bay = command['bay']
        limits = command['available_watts'], command['service_aisle_mm']
        if not bay or any(type(v) is not int or v < 0 for v in limits):
            return {'decision': 'invalid_bay'}
        added = db.execute('INSERT OR IGNORE INTO facility_bays VALUES(?,?,?)', (bay, *limits))
        return {'decision': 'bay_registered' if added.rowcount else 'bay_exists'}
    if action == 'register_freight':
        threshold = command['claim_threshold_g']
        if not command['contract'] or type(threshold) is not int or threshold <= 0:
            return {'decision': 'invalid_freight_contract'}
        added = db.execute('INSERT OR IGNORE INTO freight_contracts VALUES(?,?)', (command['contract'], threshold))
        return {'decision': 'freight_registered' if added.rowcount else 'freight_exists'}
    if action not in ('energize', 'service', 'claim'):
        return {'decision': 'unknown_operation'}
    device = observed(db, command['serial'])
    if not device:
        return {'decision': 'appliance_unobserved'}
    if action == 'claim':
        contract = one(db, 'SELECT * FROM freight_contracts WHERE id=?', (command['contract'],))
        if not contract:
            return {'decision': 'unknown_freight_contract'}
        eligible = device['shock_g'] >= contract['claim_threshold_g']
        return {'decision': 'claim_eligible' if eligible else 'claim_threshold_not_met'}
    bay = one(db, 'SELECT * FROM facility_bays WHERE id=?', (command['bay'],))
    if not bay:
        return {'decision': 'unknown_bay'}
    if action == 'energize':
        fits = device['running_watts'] <= bay['available_watts']
        return {'decision': 'energize_allowed' if fits else 'power_insufficient'}
    fits = device['clearance_mm'] <= bay['service_aisle_mm']
    return {'decision': 'service_in_place' if fits else 'remove_for_service'}
