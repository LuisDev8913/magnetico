# coding=utf-8
DEFAULT_MAX_METADATA_SIZE = 10 * 1024 * 1024
BOOTSTRAPPING_NODES = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881)
]
PENDING_INFO_HASHES = 10

TICK_INTERVAL = 1  # in seconds (soft constraint)
 # maximum (inclusive) number of active (disposable) peers to fetch the metadata per info hash at the same time:
MAX_ACTIVE_PEERS_PER_INFO_HASH = 5
