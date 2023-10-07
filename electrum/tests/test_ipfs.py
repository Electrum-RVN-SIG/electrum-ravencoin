import asyncio

from electrum import SimpleConfig
from electrum import Network
from electrum import util
from electrum import ipfs_db
from electrum import logging
from electrum.util import OldTaskGroup

if __name__ == '__main__':
    loop, stop_loop, loop_thread = util.create_and_start_event_loop()
    network = Network(SimpleConfig({'electrum_path': '/home/work/Desktop/test',
                                    'download_ipfs_preview': True,
                                    'verbosity': True,
                                    'download_ipfs_max_size': 1024 * 1024 * 1024 * 1024}))
    ipfs_db.IPFSDB.initialize(network.config.get_ipfs_data_path(), network.config.get_ipfs_raw_path())
    db = ipfs_db.IPFSDB.get_instance()
    #ipfs_hashes = ['QmUuSYPSULsPxW15gs4LPYpei78tZ1EZ5jiLQL13huoPzi', 
    #               'QmaSxufBEa9nGaoC5XTtECMmT8t5YNGcJrNcj7uWFqTkSD', 
    #               'QmQPeNsJPyVWPFDVHb77w8G42Fvo15z4bG2X8D2GhfbSXc',
    #               'Qmbj2iReDTEfWbu1iKh37soYuMARq6QobC2Zc2CcrMR4Mr',
    #               'QmdBPr3SrgGsXhBeWhTziPzst8ES7e1bvnr3CQido6gLk8']
    ipfs_hashes = [
        'QmNghWLj86SfqeWezdUpojjpQHBusNb7mAaw9rtzysyPFt'
    ]
    
    logging.configure_logging(network.config, log_to_file=False)

    async def run_test():
        network._was_started = True
        network._jobs = []
        network.taskgroup = OldTaskGroup()
        for hash in ipfs_hashes:
            await db.maybe_get_info_for_ipfs_hash(network, hash, 'test')
        await network.taskgroup.join()
        await network.stop()
        for hash in ipfs_hashes:
            print(db.get_metadata(hash))

    asyncio.run_coroutine_threadsafe(run_test(), loop).result()

    loop.call_soon_threadsafe(stop_loop.set_result, 1)
    loop_thread.join(timeout=1)
