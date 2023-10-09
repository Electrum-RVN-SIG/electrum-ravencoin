import asyncio

from typing import Sequence, Callable, Tuple, Optional, Mapping

from multiformats import CID, multicodec
import dag_cbor

# Basically taken from:
# https://github.com/ipld/js-car
# https://github.com/ipfs/js-ipfs-unixfs/blob/master/packages/ipfs-unixfs-exporter/src/index.ts

_MSB = 0x80
_REST = 0x7f
_V2_HEADER_LENGTH = 40
_CIDV0_SHA2_256 = 0x12
_CIDV0_LENGTH = 0x20
_CIDV0_DAG_PB = 0x70

class AsyncByteStreamHolder:
    def __init__(self):
        self._complete = False
        self._pos = 0
        self._limit = None
        self._bytes = bytearray()
        self._added_bytes_cond = asyncio.Condition()

    def move(self, position: int) -> None:
        self._pos = position

    def can_read_more(self) -> bool:
        return self._pos < (self._limit or (len(self._bytes) - 1))

    async def mark_complete(self) -> None:
        self._complete = True
        async with self._added_bytes_cond:
            self._added_bytes_cond.notify_all()

    async def append_bytes(self, b: bytes) -> None:
        if self._complete:
            raise Exception('complete flag was set!')
        assert isinstance(b, bytes)
        self._bytes.extend(b)
        async with self._added_bytes_cond:
            self._added_bytes_cond.notify_all()

    async def read_u8(self) -> int:
        return (await self.read_bytes(1))[0]

    # https://github.com/chrisdickinson/varint/blob/master/decode.js
    async def read_var_int(self) -> int:
        result = 0
        shift = 0
        while True:
            if shift > 49:
                raise Exception(f'cannot decode varint')
            num = await self.read_u8()
            result += ((num & _REST) << shift) if (shift < 28) else ((num & _REST) * pow(2, shift))
            shift += 7
            if num < _MSB: break
        return result 

    async def read_bytes(self, count: int, walk_forward=True) -> bytes:
        result = await self.read_slice(self._pos, count + self._pos)
        assert len(result) == count, f'expected {count} bytes, got {len(result)}'
        if walk_forward:
            self._pos += count
        return result

    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        assert self.can_read_more()
        if self._limit and end_exclusive > self._limit:
            raise Exception('limit will be breached')
        while end_exclusive > len(self._bytes):
            if self._complete:
                raise Exception('waiting for bytes, but complete flag was set!')
            else:
                async with self._added_bytes_cond:
                    await self._added_bytes_cond.wait()
        return bytes(self._bytes[start:end_exclusive])
     
class BlockIndex:
    def __init__(self, cid: CID, length: int, block_length: int, offset: int, block_offset: int):
        self.cid = cid
        self.length = length
        self.block_length = block_length
        self.offset = offset
        self.block_offset = block_offset

async def write_raw_from_car(stream: AsyncByteStreamHolder, ipfs_hash: str):
    header_length = await stream.read_var_int()
    assert header_length > 0
    raw_header = await stream.read_bytes(header_length)
    header = dag_cbor.decode(raw_header)
    if header['version'] not in (1, 2):
        raise Exception(f'unknown car version {header["version"]}')
    if header['version'] == 1:
        assert isinstance(header['roots'], Sequence)
    else:
        assert 'roots' not in header
        v2_header_raw = await stream.read_bytes(_V2_HEADER_LENGTH)
        btole64 = lambda b: int.from_bytes(b, 'little')
        v2_header = {
            'version': 2,
            'characteristics': [
                btole64(v2_header_raw[:8]),
                btole64(v2_header_raw[8:16])
            ],
            'data_offset': btole64(v2_header_raw[16:24]),
            'data_size': btole64(v2_header_raw[24:32]),
            'index_offset': btole64(v2_header_raw[32:40])
        }
        stream.move(v2_header['data_offset'])
        v1_header_length = await stream.read_var_int()
        assert v1_header_length > 0
        v1_header_raw = await stream.read_bytes(v1_header_length)
        header = dag_cbor.decode(v1_header_raw)
        assert header['version'] == 1
        assert isinstance(header['roots'], Sequence)
        header.update(v2_header)
        stream._limit = header['data_size'] + header['data_offset']

    block_indices: Sequence[BlockIndex] = []
    while stream.can_read_more():
        offset = stream._pos
        length = await stream.read_var_int()
        assert length > 0
        length += (stream._pos - offset)
        first = await stream.read_bytes(2, walk_forward=False)
        if first[0] == _CIDV0_SHA2_256 and first[1] == _CIDV0_LENGTH:
            raw_multihash = await stream.read_bytes(34)
            assert len(raw_multihash) == 34
            cid = CID.decode(raw_multihash)
        else:
            version = await stream.read_var_int()
            assert version == 1
            codec = await stream.read_var_int()
            multihash_code = await stream.read_var_int()
            multihash_length = await stream.read_var_int()
            raw_hash = await stream.read_bytes(multihash_length)
            assert len(raw_hash) == multihash_length
            cid = CID('base32', version, codec, (multihash_code, raw_hash))
        block_length = length - (stream._pos - offset)
        block_indices.append(
            BlockIndex(cid, length, block_length, offset, stream._pos)
        )
        hashing_function, digest_size = cid.hashfun.implementation
        raw = await stream.read_bytes(block_length)
        computed_hash = hashing_function(raw)
        assert computed_hash == cid.raw_digest

    cid_mapping = {}
    cid_order = []
    for block_index in block_indices:
        cid_str = block_index.cid.encode()
        cid_mapping[cid_str] = (block_index.block_length, block_index.block_offset)
        cid_order.append(cid_str)

    assert cid_order[0] == ipfs_hash

