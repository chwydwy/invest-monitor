"""Company matching and disclosure Telegram routing."""

import json
from pathlib import Path

_alias_path = Path(__file__).resolve().parent.parent / "DASHBOARD_COMPANY_ALIASES.json"
with _alias_path.open(encoding="utf-8") as _source:
    _alias_catalog = json.load(_source)

CONFIRMED_ALIASES = {}
for _company in _alias_catalog["companies"]:
    for _name in [_company["name"], *_company["aliases"]]:
        _existing = CONFIRMED_ALIASES.setdefault(_name, _company["name"])
        if _existing != _company["name"]:
            raise ValueError(f"Conflicting company alias: {_name}")


def load_list(filename):
    path = Path(filename)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as source:
        return [line.strip() for line in source if line.strip()]


def company_identity(name):
    return CONFIRMED_ALIASES.get(name, name)


def review_bl_update(current_bl, current_jm, proposed_bl):
    """Describe a proposed BL replacement without changing either source list."""
    old_ids = {company_identity(name) for name in current_bl}
    new_ids = {company_identity(name) for name in proposed_bl}
    jm_ids = {company_identity(name) for name in current_jm}
    return {
        "bl_additions": [name for name in proposed_bl if company_identity(name) not in old_ids | jm_ids],
        "bl_removals": [name for name in current_bl if company_identity(name) not in new_ids],
        "move_from_jm_to_bl": [name for name in current_jm if company_identity(name) in new_ids],
        "jm_retained": [name for name in current_jm if company_identity(name) not in new_ids],
    }


def matches_group(names, company, submitter, market_notice):
    identity = company_identity(company)
    for name in names:
        if company_identity(name) == identity:
            return company_identity(submitter) == identity or market_notice
    return False


def disclosure_rooms(config, in_universe, in_chem):
    """Return each destination once; BL receives the full universe."""
    rooms = []
    for selected, key in ((in_universe, "CHAT_ID_BL"), (in_chem, "CHAT_ID_CHEM")):
        if selected and config[key] not in rooms:
            rooms.append(config[key])
    return rooms
