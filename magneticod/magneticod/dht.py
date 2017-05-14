# magneticod - Autonomous BitTorrent DHT crawler and metadata fetcher.
# Copyright (C) 2017  Mert Bora ALPER <bora@boramalper.org>
# Dedicated to Cemile Binay, in whose hands I thrived.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
import array
import asyncio
import collections
import itertools
import zlib
import logging
import socket
import typing
import os

from .constants import BOOTSTRAPPING_NODES, DEFAULT_MAX_METADATA_SIZE, MAX_ACTIVE_PEERS_PER_INFO_HASH
from . import bencode
from . import bittorrent

NodeID = bytes
NodeAddress = typing.Tuple[str, int]
PeerAddress = typing.Tuple[str, int]
InfoHash = bytes


class SybilNode:
    def __init__(self, address: typing.Tuple[str, int], complete_info_hashes, max_metadata_size):
        self.__true_id = self.__random_bytes(20)

        self.__address = address

        self._routing_table = {}  # type: typing.Dict[NodeID, NodeAddress]

        self.__token_secret = self.__random_bytes(4)
        # Maximum number of neighbours (this is a THRESHOLD where, once reached, the search for new neighbours will
        # stop; but until then, the total number of neighbours might exceed the threshold).
        self.__n_max_neighbours = 2000
        self.__peers = collections.defaultdict(
            list)  # type: typing.DefaultDict[dht.InfoHash, typing.List[bittorrent.DisposablePeer]]
        self._complete_info_hashes = complete_info_hashes
        self.__max_metadata_size = max_metadata_size
        self._metadata_q = asyncio.Queue()

        logging.info("SybilNode %s on %s initialized!", self.__true_id.hex().upper(), address)

    async def launch(self, loop):
        self._loop = loop
        await loop.create_datagram_endpoint(lambda: self, local_addr=self.__address)

    def connection_made(self, transport):
        self._loop.create_task(self.on_tick())
        self._loop.create_task(self.increase_neighbour_task())
        self._transport = transport

    def error_received(self, exc):
        logging.error("got error %s", exc)
        if isinstance(exc, PermissionError):
            # In case of congestion, decrease the maximum number of nodes to the 90% of the current value.
            if self.__n_max_neighbours < 200:
                logging.warning("Maximum number of neighbours are now less than 200 due to congestion!")
            else:
                self.__n_max_neighbours = self.__n_max_neighbours * 9 // 10
                logging.debug("Maximum number of neighbours now %d", self.__n_max_neighbours)

    async def on_tick(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.__bootstrap()
            self.__make_neighbours()
            self._routing_table.clear()

    def datagram_received(self, data, addr) -> None:
        # Ignore nodes that uses port 0 (assholes).
        if addr[1] == 0:
            return

        try:
            message = bencode.loads(data)
        except bencode.BencodeDecodingError:
            return

        if isinstance(message.get(b"r"), dict) and type(message[b"r"].get(b"nodes")) is bytes:
            self.__on_FIND_NODE_response(message)
        elif message.get(b"q") == b"get_peers":
            self.__on_GET_PEERS_query(message, addr)
        elif message.get(b"q") == b"announce_peer":
            self.__on_ANNOUNCE_PEER_query(message, addr)

    async def increase_neighbour_task(self):
        while True:
            await asyncio.sleep(10)
            self.__n_max_neighbours = self.__n_max_neighbours * 101 // 100

    def shutdown(self) -> None:
        for peer in itertools.chain.from_iterable(self.__peers.values()):
            peer.close()
        self._transport.close()

    def __on_FIND_NODE_response(self, message: bencode.KRPCDict) -> None:
        try:
            nodes_arg = message[b"r"][b"nodes"]
            assert type(nodes_arg) is bytes and len(nodes_arg) % 26 == 0
        except (TypeError, KeyError, AssertionError):
            return

        try:
            nodes = self.__decode_nodes(nodes_arg)
        except AssertionError:
            return

        # Add new found nodes to the routing table, assuring that we have no more than n_max_neighbours in total.
        if len(self._routing_table) < self.__n_max_neighbours:
            self._routing_table.update(nodes)

    def __on_GET_PEERS_query(self, message: bencode.KRPCDict, addr: NodeAddress) -> None:
        try:
            transaction_id = message[b"t"]
            assert type(transaction_id) is bytes and transaction_id
            info_hash = message[b"a"][b"info_hash"]
            assert type(info_hash) is bytes and len(info_hash) == 20
        except (TypeError, KeyError, AssertionError):
            return

        data = bencode.dumps({
            b"y": b"r",
            b"t": transaction_id,
            b"r": {
                b"id": info_hash[:15] + self.__true_id[:5],
                b"nodes": b"",
                b"token": self.__calculate_token(addr, info_hash)
            }
        })
        # we want to prioritise GET_PEERS responses as they are the most fruitful ones!
        # but there is no easy way to do this with asyncio
        self._transport.sendto(data, addr)

    def __on_ANNOUNCE_PEER_query(self, message: bencode.KRPCDict, addr: NodeAddress) -> None:
        try:
            node_id = message[b"a"][b"id"]
            assert type(node_id) is bytes and len(node_id) == 20
            transaction_id = message[b"t"]
            assert type(transaction_id) is bytes and transaction_id
            token = message[b"a"][b"token"]
            assert type(token) is bytes
            info_hash = message[b"a"][b"info_hash"]
            assert type(info_hash) is bytes and len(info_hash) == 20
            if b"implied_port" in message[b"a"]:
                implied_port = message[b"a"][b"implied_port"]
                assert implied_port in (0, 1)
            else:
                implied_port = None
            port = message[b"a"][b"port"]

            assert type(port) is int and 0 < port < 65536
        except (TypeError, KeyError, AssertionError):
            return

        data = bencode.dumps({
            b"y": b"r",
            b"t": transaction_id,
            b"r": {
                b"id": node_id[:15] + self.__true_id[:5]
            }
        })
        self._transport.sendto(data, addr)

        if implied_port:
            peer_addr = (addr[0], addr[1])
        else:
            peer_addr = (addr[0], port)

        if len(self.__peers[info_hash]) > MAX_ACTIVE_PEERS_PER_INFO_HASH or \
           info_hash in self._complete_info_hashes:
            return

        peer = bittorrent.get_torrent_data(info_hash, peer_addr, self.__max_metadata_size)
        self.__peers[info_hash].append(peer)
        self._loop.create_task(peer).add_done_callback(self.metadata_found)

    def metadata_found(self, future):
        r = future.result()
        if r:
            info_hash, metadata = r
            for peer in self.__peers[info_hash]:
                peer.close()
            self._metadata_q.put_nowait(r)
            self._complete_info_hashes.add(info_hash)

    def __bootstrap(self) -> None:
        for addr in BOOTSTRAPPING_NODES:
            data = self.__build_FIND_NODE_query(self.__true_id)
            self._transport.sendto(data, addr)

    def __make_neighbours(self) -> None:
        for node_id, addr in self._routing_table.items():
            self._transport.sendto(self.__build_FIND_NODE_query(node_id[:15] + self.__true_id[:5]), addr)

    @staticmethod
    def __decode_nodes(infos: bytes) -> typing.List[typing.Tuple[NodeID, NodeAddress]]:
        """ REFERENCE IMPLEMENTATION
        nodes = []
        for i in range(0, len(infos), 26):
            info = infos[i: i + 26]
            node_id = info[:20]
            node_host = socket.inet_ntoa(info[20:24])
            node_port = int.from_bytes(info[24:], "big")
            nodes.append((node_id, (node_host, node_port)))
        return nodes
        """

        """ Optimized Version """
        inet_ntoa = socket.inet_ntoa
        int_from_bytes = int.from_bytes
        return [
            (infos[i:i+20], (inet_ntoa(infos[i+20:i+24]), int_from_bytes(infos[i+24:i+26], "big")))
            for i in range(0, len(infos), 26)
        ]

    def __calculate_token(self, addr: NodeAddress, info_hash: InfoHash):
        # Believe it or not, faster than using built-in hash (including conversion from int -> bytes of course)
        return zlib.adler32(b"%s%s%d%s" % (self.__token_secret, socket.inet_aton(addr[0]), addr[1], info_hash))

    @staticmethod
    def __random_bytes(n: int) -> bytes:
        return os.urandom(n)

    def __build_FIND_NODE_query(self, id_: bytes) -> bytes:
        """ BENCODE IMPLEMENTATION
        bencode.dumps({
            b"y": b"q",
            b"q": b"find_node",
            b"t": self.__random_bytes(2),
            b"a": {
                b"id": id_,
                b"target": self.__random_bytes(20)
            }
        })
        """

        """ Optimized Version """
        return b"d1:ad2:id20:%s6:target20:%se1:q9:find_node1:t2:aa1:y1:qe" % (
            id_,
            self.__random_bytes(20)
        )
