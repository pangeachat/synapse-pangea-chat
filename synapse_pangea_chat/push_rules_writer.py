"""The one Synapse process that can write push rules.

Synapse has no replication path for push-rule writes: on any process but the
push rules writer ("master" on a single process) they raise "Not a push
writer". Work that writes one runs there and nowhere else.
"""

from synapse.module_api import ModuleApi


def push_rules_writer(api: ModuleApi) -> str:
    return api._hs.config.worker.writers.push_rules[0]


def writes_push_rules(api: ModuleApi) -> bool:
    return api._hs.get_instance_name() == push_rules_writer(api)
