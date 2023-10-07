import json
import os
import threading
import stat
import attr
import asyncio
import time
import itertools

from base64 import b32encode
from typing import TYPE_CHECKING, Set, Dict, Optional

from aiohttp import ClientResponse
from aiorpcx import run_in_thread
from collections import defaultdict

from .bitcoin import base_decode
from .json_db import JsonDB, locked, modifier, StoredObject, StoredDict
from .util import (standardize_path, test_read_write_permissions, profiler, os_chmod, 
                   ipfs_explorer_URL, ipfs_explorer_round_robin, event_listener, make_dir, EventListener)
from .network import Network

from electrum import util

if TYPE_CHECKING:
    from .address_synchronizer import AddressSynchronizer

_VIEWABLE_MIMES = ('image/*', 'text/plain', 'application/json')

_LOOKUP_COOLDOWN_SEC = 60

def is_mime_viewable(mime_type: str) -> bool:
    if not mime_type: return False
    for good in _VIEWABLE_MIMES:
        if '*' in good:
            start = good.split('*')[0]
            if mime_type.startswith(start):
                return True
        else:
            if good == mime_type:
                return True
    return False

def human_readable_size(size, decimal_places=3, greater_than=False):
    if not isinstance(size, int):
        return 'Unknown'
    for unit in ['Bytes','KiB','MiB','GiB','TiB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{'>' if greater_than else ''}{size:.{decimal_places}g} {unit}"

def cidv0_to_base32_cidv1(b58_ipfs_hash: str):
    multibase = base_decode(b58_ipfs_hash, base=58)
    assert len(multibase) == 34

    # https://github.com/multiformats/multibase
    # https://github.com/multiformats/multicodec
    # https://github.com/multiformats/multihash

    # v0: multihash

    # v1: multiformat prefix (varint) + encoded(version (varint) + multicodec prefix (varint) + multhash)
    # b: base32 (padding omitted) multiformat prefix
    # 1: version
    # 0x70 is dag-pb multicodec; what is used for IPFS lookups

    # note that the IPFS varint is different than the bitcoin varint

    result = 'b' + b32encode(bytes([1, 0x70]) + multibase).decode('ascii').lower().replace('=', '')
    assert result.isalnum(), result
    return result

@attr.s
class IPFSMetadata(StoredObject):
    known_size = attr.ib(default=None, type=Optional[int], validator=attr.validators.optional(attr.validators.instance_of(int)))
    over_sized = attr.ib(default=False, type=bool, validator=attr.validators.instance_of(bool))
    known_mime = attr.ib(default=None, type=Optional[str], validator=attr.validators.optional(attr.validators.instance_of(str)))
    is_client_side = attr.ib(default=False, type=bool, validator=attr.validators.instance_of(bool))
    last_attemped_info_query = attr.ib(default=None, type=Optional[int], validator=attr.validators.optional(attr.validators.instance_of(int)))
    last_attemped_data_download = attr.ib(default=None, type=Optional[int], validator=attr.validators.optional(attr.validators.instance_of(int)))
    info_lookup_successful = attr.ib(default=False, type=bool, validator=attr.validators.instance_of(bool))
    associated_assets = attr.ib(factory=set, type=Set[str], converter=set)

class IPFSDBReadWriteError(Exception): pass

class IPFSDB(JsonDB, EventListener):
    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, 'instance'):
            cls._instance = super().__new__(cls)
            cls._instance.__init__(*args, **kwargs)
        return cls._instance

    @classmethod
    def initialize(cls, path: str, raw_path: str):
        cls(path, raw_path).logger.info('loaded IPFS database')

    @classmethod
    def get_instance(cls) -> 'IPFSDB':
        assert cls._instance
        return cls._instance

    def __init__(self, path: str, raw_path: str):
        JsonDB.__init__(self, {})
        self.path = standardize_path(path)
        self._file_exists = bool(self.path and os.path.exists(self.path))
        try:
            test_read_write_permissions(self.path)
        except IOError as e:
            raise IPFSDBReadWriteError(e) from e
        if self.file_exists():
            with open(self.path, "r", encoding='utf-8') as f:
                raw = f.read()
                self.data: Dict[str, IPFSMetadata] = StoredDict(json.loads(raw), self, [])

        self.raw_ipfs_path = standardize_path(raw_path)
        make_dir(self.raw_ipfs_path, False)

        self._ipfs_single_gateway_semaphore = asyncio.Semaphore(5)
        self._ipfs_gateway_locks = defaultdict(asyncio.Lock)
        self._ipfs_lookup_current = set()
        self._ipfs_download_current = set()

        self.register_callbacks()
    
    def _should_convert_to_stored_dict(self, key) -> bool:
        return False

    def _local_path_for_ipfs_data(self, ipfs_hash: str):
        return standardize_path(os.path.join(self.raw_ipfs_path, f'{cidv0_to_base32_cidv1(ipfs_hash)}.dat'))

    @locked
    @profiler
    def write(self) -> None:
        if threading.current_thread().daemon:
            self.logger.warning('daemon thread cannot write db')
            return
        if not self.modified():
            return
        temp_path = "%s.tmp.%s" % (self.path, os.getpid())
        with open(temp_path, "w", encoding='utf-8') as f:
            json_str = self.dump()
            f.write(json_str)
            f.flush()
            os.fsync(f.fileno())

        try:
            mode = os.stat(self.path).st_mode
        except FileNotFoundError:
            mode = stat.S_IREAD | stat.S_IWRITE

        # assert that wallet file does not exist, to prevent wallet corruption (see issue #5082)
        if not self.file_exists():
            assert not os.path.exists(self.path)
        os.replace(temp_path, self.path)
        os_chmod(self.path, mode)
        self._file_exists = True
        self.logger.info(f"saved {self.path}")
        self.set_modified(False)

    def file_exists(self) -> bool:
        return self._file_exists
    
    def _convert_dict(self, path, key, v):
        v = IPFSMetadata(**v)
        v.set_db(self)
        return v
    
    def _append_bytes_to_raw_ipfs_file(self, ipfs_hash: str, b: bytes):
        ipfs_file = self._local_path_for_ipfs_data(ipfs_hash)
        with open(ipfs_file, 'ab') as f:
            f.write(b)

    @locked
    def purge_stale_ipfs_data(self):
        valid_files = {self._local_path_for_ipfs_data(ipfs_hash) for ipfs_hash in self.data.keys()}
        for _, _, filenames in os.walk(self.raw_ipfs_path):
            for file in filenames:
                path = os.path.join(self.raw_ipfs_path, file)
                if path not in valid_files:
                    os.remove(path)
        
        stale_hashes = {ipfs_hash for ipfs_hash in self.data.keys() if not self.data[ipfs_hash].associated_assets}
        for hash in stale_hashes:
            self.remove_ipfs_info(hash)
        self.logger.info(f'pruned {len(stale_hashes)} stale ipfs data sets')

    @modifier
    def remove_ipfs_info(self, ipfs_hash: str):
        self.data.pop(ipfs_hash)
        self.remove_ipfs_data(ipfs_hash)

    @locked
    def get_total_bytes_on_disk(self):
        total_size = 0
        for _, _, filenames in os.walk(self.raw_ipfs_path):
            for file in filenames:
                path = os.path.join(self.raw_ipfs_path, file)
                if not os.path.islink(path):
                    total_size += os.path.getsize(path)
        return total_size

    @modifier
    def clear_cache(self):
        for ipfs_hash, metadata in self.data.items():
            metadata.is_client_side = False
            self.remove_ipfs_data(ipfs_hash)

    def remove_ipfs_data(self, ipfs_hash: str):
        ipfs_file = self._local_path_for_ipfs_data(ipfs_hash)
        try:
            os.remove(ipfs_file)
        except (FileNotFoundError, OSError):
            pass

    async def _download_ipfs_data(self, network: Network, ipfs_hash: str):        
        async def on_finish(resp: ClientResponse):
            m = self.get_metadata(ipfs_hash)
            try:
                resp.raise_for_status()
                # Ensure we aren't appending to something thats already there
                self.remove_ipfs_data(ipfs_hash)
                downloaded_length = 0
                async for chunk, _ in resp.content.iter_chunks():
                    downloaded_length += len(chunk)
                    if downloaded_length > network.config.MAX_IPFS_DOWNLOAD_SIZE:
                        self.logger.warning(f'oversized ipfs data for {ipfs_hash}')
                        m.over_sized = True
                        m.known_size = downloaded_length
                        self.remove_ipfs_data(ipfs_hash)
                        break
                    await run_in_thread(
                        self._append_bytes_to_raw_ipfs_file,
                        ipfs_hash,
                        chunk
                    )
                else:
                    self.logger.info(f'successfully downloaded ipfs data for {ipfs_hash}')
                    m.known_size = downloaded_length
                    m.is_client_side = True
            finally:
                resp.close()

        async def lookup_data(ipfs_url: str):
            await asyncio.sleep(0.25)
            self.logger.info(f'attempting to download data from {ipfs_url}')
            await Network.async_send_http_on_proxy('get', 
                                                   ipfs_url, 
                                                   on_finish=on_finish, 
                                                   timeout=network.config.MAX_IPFS_DOWNLOAD_WAIT,
                                                   headers={
                                                       'Accept': 'application/vnd.ipld.car'
                                                   })

        try:
            self.logger.info(f'downloading ipfs data for {ipfs_hash}')
            ipfs_url_safe = cidv0_to_base32_cidv1(ipfs_hash)
            if network.config.ROUND_ROBIN_ALL_KNOWN_IPFS_GATEWAYS:
                ipfs_urls = {name: url for name, url in ipfs_explorer_round_robin(network.config, 'ipfs', ipfs_url_safe)}
                tried_gateways = set()
                while tried_gateways != set(ipfs_urls.keys()):
                    async def get_gateway(gateway: str):
                        lock = self._ipfs_gateway_locks[gateway]
                        try:
                            await lock.acquire()
                        finally:
                            return gateway
                    
                    # Need to populate default dict if doesnt exist
                    locks = [asyncio.create_task(get_gateway(gateway)) 
                             for gateway in ipfs_urls.keys() if gateway not in tried_gateways]
                    completed, pending = await asyncio.wait(locks, return_when=asyncio.FIRST_COMPLETED)
                    completed_l = list(completed)

                    # Immediately free unused locks for other tasks (is this needed?)
                    for locking_task in itertools.chain(completed_l[1:], pending):
                        def on_done(task: asyncio.Task):
                            gateway = task.result()
                            self._ipfs_gateway_locks[gateway].release()

                        locking_task.add_done_callback(on_done)

                    gateway = completed_l[0].result()
                    url = ipfs_urls[gateway]
                    try:
                        await lookup_data(url)
                        return
                    except asyncio.TimeoutError:
                        self.logger.warning(f'timeout trying to download ipfs data from {url}')
                    except Exception as e:
                        self.logger.warning(f'failed to download data from {url}: {str(e)} ({e.__class__})')
                    finally:
                        tried_gateways.add(gateway)
                        self._ipfs_gateway_locks[gateway].release()

            else:
                url = ipfs_explorer_URL(network.config, 'ipfs', ipfs_hash)
                try:
                    async with self._ipfs_single_gateway_semaphore:
                        await lookup_data(url)
                except asyncio.TimeoutError:
                    self.logger.warning(f'timeout trying to lookup ipfs info from {url}')
                except Exception as e:
                    self.logger.warning(f'failed to download data from {url}: {str(e)} ({e.__class__})')
        finally:
            curr_time = int(time.time())
            m = self.get_metadata(ipfs_hash)
            m.last_attemped_data_download = curr_time
            self._modified = True
            self._ipfs_download_current.discard(ipfs_hash)
            util.trigger_callback('ipfs_download', ipfs_hash)

    async def _download_ipfs_information(self, network: Network, ipfs_hash: str):
        async def on_finish(resp: ClientResponse):
            m = self.get_metadata(ipfs_hash)
            try:
                resp.raise_for_status()
                if resp.content_type == 'application/octet-stream' and not resp.content_length:
                    raise Exception('HEAD response returned default data')
                m.known_mime = resp.content_type
                m.known_size = resp.content_length
                m.info_lookup_successful = True
                self.logger.info(f'downloaded information for ipfs {ipfs_hash}: {resp.content_type} {resp.content_length}')
                                
                # Lookup current needs to be cleared here because
                # its checked in download data
                self._ipfs_lookup_current.discard(ipfs_hash)
                await self.maybe_download_data_for_ipfs_hash(network, ipfs_hash)
            finally:
                resp.close()

        async def lookup_info(ipfs_url: str):
            await asyncio.sleep(0.25)
            self.logger.info(f'attempting to download info from {ipfs_url}')
            await Network.async_send_http_on_proxy('head', ipfs_url, on_finish=on_finish, timeout=network.config.MAX_IPFS_DOWNLOAD_WAIT)

        try:
            self.logger.info(f'looking up ipfs info for {ipfs_hash}')
            ipfs_url_safe = cidv0_to_base32_cidv1(ipfs_hash)
            if network.config.ROUND_ROBIN_ALL_KNOWN_IPFS_GATEWAYS:
                ipfs_urls = {name: url for name, url in ipfs_explorer_round_robin(network.config, 'ipfs', ipfs_url_safe)}
                tried_gateways = set()
                while tried_gateways != set(ipfs_urls.keys()):
                    async def get_gateway(gateway: str):
                        lock = self._ipfs_gateway_locks[gateway]
                        try:
                            await lock.acquire()
                        finally:
                            return gateway
                    
                    # Need to populate default dict if doesnt exist
                    locks = [asyncio.create_task(get_gateway(gateway))
                             for gateway in ipfs_urls.keys() if gateway not in tried_gateways]
                    completed, pending = await asyncio.wait(locks, return_when=asyncio.FIRST_COMPLETED)
                    completed_l = list(completed)

                    # Immediately free unused locks for other tasks (is this needed?)
                    for locking_task in itertools.chain(completed_l[1:], pending):
                        def on_done(task: asyncio.Task):
                            gateway = task.result()
                            self._ipfs_gateway_locks[gateway].release()

                        locking_task.add_done_callback(on_done)

                    gateway = completed_l[0].result()
                    url = ipfs_urls[gateway]
                    try:
                        await lookup_info(url)
                        return
                    except asyncio.TimeoutError:
                        self.logger.warning(f'timeout trying to download ipfs info from {url}')
                    except Exception as e:
                        self.logger.warning(f'failed to download info from {url}: {str(e)} ({e.__class__})')
                    finally:
                        tried_gateways.add(gateway)
                        self._ipfs_gateway_locks[gateway].release()

            else:
                url = ipfs_explorer_URL(network.config, 'ipfs', ipfs_hash)
                try:
                    async with self._ipfs_single_gateway_semaphore:
                        await lookup_info(url)
                except asyncio.TimeoutError:
                    self.logger.warning(f'timeout trying to lookup ipfs info from {url}')
                except Exception as e:
                    self.logger.warning(f'failed to download info from {url}: {str(e)} ({e.__class__})')
        finally:
            curr_time = int(time.time())
            m = self.get_metadata(ipfs_hash)
            if m:
                m.last_attemped_info_query = curr_time
                self._modified = True
                self._ipfs_lookup_current.discard(ipfs_hash)
                util.trigger_callback('ipfs_download', ipfs_hash)

    @modifier
    async def maybe_download_data_for_ipfs_hash(self, network: 'Network', ipfs_hash: str):
        assert isinstance(ipfs_hash, str)
        if not network:
            return
        raw_hash = base_decode(ipfs_hash, base=58)
        if len(raw_hash) != 34 or raw_hash[:2] != b'\x12\x20':
            raise ValueError(f'Invalid ipfs hash: {ipfs_hash}')
        if not network.config.DOWNLOAD_IPFS: return
        with self.lock:
            m = self.get_metadata(ipfs_hash)
            # get info calls this
            if ipfs_hash in self._ipfs_lookup_current:
                return
            if ipfs_hash in self._ipfs_download_current:
                return
            
            if m and m.info_lookup_successful and not m.is_client_side and \
                is_mime_viewable(m.known_mime) and (m.known_size is None or \
                                                     m.known_size < network.config.MAX_IPFS_DOWNLOAD_SIZE):
                
                curr_time = int(time.time())
                if m and m.last_attemped_data_download and (m.last_attemped_data_download + _LOOKUP_COOLDOWN_SEC) > curr_time:
                    self.logger.info(f'Not downloading data for {ipfs_hash}: cooling down')
                    return
            
                self._ipfs_download_current.add(ipfs_hash)
                await network.taskgroup.spawn(self._download_ipfs_data(network, ipfs_hash))

    async def maybe_get_info_for_ipfs_hash(self, network: 'Network', ipfs_hash: str, asset: str):
        assert isinstance(ipfs_hash, str)
        assert isinstance(asset, str)
        if not network:
            return
        raw_hash = base_decode(ipfs_hash, base=58)
        if len(raw_hash) != 34 or raw_hash[:2] != b'\x12\x20':
            raise ValueError(f'Invalid ipfs hash: {ipfs_hash} ({ipfs_hash.__class__})')
        self.associate_asset_with_ipfs(ipfs_hash, asset)
        if not network.config.DOWNLOAD_IPFS: return
        with self.lock:
            m = self.get_metadata(ipfs_hash)
            if m.info_lookup_successful:
                return
            if ipfs_hash in self._ipfs_lookup_current:
                return
            curr_time = int(time.time())
            if m.last_attemped_info_query and (m.last_attemped_info_query + _LOOKUP_COOLDOWN_SEC) > curr_time:
                self.logger.info(f'Not downloading information for {ipfs_hash}: cooling down')
                return
            self._ipfs_lookup_current.add(ipfs_hash)
            util.trigger_callback('ipfs_download', ipfs_hash)
            await network.taskgroup.spawn(self._download_ipfs_information(network, ipfs_hash))

    @event_listener
    async def on_event_adb_added_verified_asset_metadata(self, adb: 'AddressSynchronizer', asset):
        metadata = adb.db.get_verified_asset_metadata(asset)
        if metadata and metadata.is_associated_data_ipfs():
            await self.maybe_get_info_for_ipfs_hash(adb.network, metadata.associated_data_as_ipfs(), asset)

    @event_listener
    async def on_event_adb_added_unconfirmed_asset_metadata(self, adb: 'AddressSynchronizer', asset):
        metadata_tup = adb.unconfirmed_asset_metadata.get(asset, None)
        if metadata_tup:
            metadata = metadata_tup[0]
            if metadata.is_associated_data_ipfs():
                await self.maybe_get_info_for_ipfs_hash(adb.network, metadata.associated_data_as_ipfs(), asset)

    @event_listener
    async def on_event_adb_added_verified_broadcast(self, adb: 'AddressSynchronizer', asset, tx_hash):
        broadcast = adb.db.get_verified_broadcast(asset, tx_hash)
        maybe_ipfs = broadcast['data']
        if maybe_ipfs[:2] == 'Qm':
            await self.maybe_get_info_for_ipfs_hash(adb.network, maybe_ipfs, asset)

    @event_listener
    async def on_event_adb_added_unconfirmed_broadcast(self, adb: 'AddressSynchronizer', asset, tx_hash):
        broadcast = adb.get_unverified_broadcasts()[asset][tx_hash]
        maybe_ipfs = broadcast['data']
        if maybe_ipfs[:2] == 'Qm':
            await self.maybe_get_info_for_ipfs_hash(adb.network, maybe_ipfs, asset)

    @event_listener
    def on_event_ipfs_hash_dissociate_asset(self, ipfs_hash: str, asset: str):
        self.dissociate_asset_with_ipfs(ipfs_hash, asset)

    @modifier
    def dissociate_asset_with_ipfs(self, ipfs_hash: str, asset: str):
        m = self.data.get(ipfs_hash, None)
        if m:
            self.logger.info(f'disassociating {asset} from {ipfs_hash}')
            m.associated_assets.discard(asset)
            if not m.associated_assets:
                self.logger.info(f'nothing pinning {ipfs_hash}; removing')
                self.remove_ipfs_info(ipfs_hash)

    @modifier
    def associate_asset_with_ipfs(self, ipfs_hash: str, asset: str):
        m = self.data.get(ipfs_hash, None)
        if m is None:
            m = IPFSMetadata(associated_assets={asset})
            self.data[ipfs_hash] = m
        else:
            m.associated_assets.add(asset)

    @locked
    def get_metadata(self, ipfs_hash: str):
        return self.data.get(ipfs_hash, None)

    @locked
    def get_text_for_ipfs_str(self, ipfs_hash: str):
        m = self.data.get(ipfs_hash, None)
        if m is None:
            return (None, ) * 2
        if ipfs_hash in self._ipfs_lookup_current:
            return ('Loading...', ) * 2
        return m.known_mime, human_readable_size(m.known_size, greater_than=m.over_sized)

    @locked
    def get_resource_path_for_ipfs_str(self, ipfs_hash: str):
        m = self.data.get(ipfs_hash, None)
        if m is None or not m.is_client_side:
            return None, None
        path = self._local_path_for_ipfs_data(ipfs_hash)
        if not os.path.exists(path):
            return None, None
        return path, m.known_mime
