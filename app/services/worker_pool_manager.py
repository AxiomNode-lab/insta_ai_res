
import asyncio
import logging
import multiprocessing
import time
import signal
from typing import List
from app.core.config import settings
from app.services.instagram_service import process_outgoing_queue
from app.core.redis_utils import get_redis_client, ACTIVE_ACCOUNTS_KEY, set_worker_status

logger = logging.getLogger(__name__)

# --- Worker Logic ---
def run_worker_pool_process(worker_id: int, stop_event: multiprocessing.Event):
    """
    Worker Process that handles multiple accounts in Round-Robin fashion.
    """
    logging.basicConfig(level=logging.INFO, format=f'%(asctime)s [WorkerPool-{worker_id}] %(levelname)s: %(message)s')
    local_logger = logging.getLogger(f"worker_pool_{worker_id}")
    
    local_logger.info(f"Worker Pool Process {worker_id} started.")
    
    async def worker_loop():
        messages_processed = 0
        start_time = time.time()
        
        # Worker Recycling Config
        MAX_MESSAGES = 1000
        MAX_UPTIME = 3600 # 1 hour
        
        while not stop_event.is_set():
            try:
                # 1. Heartbeat
                await set_worker_status(f"pool_{worker_id}", "alive")
                
                # 2. Check Recycling Conditions
                uptime = time.time() - start_time
                if messages_processed >= MAX_MESSAGES or uptime >= MAX_UPTIME:
                    local_logger.info("Worker recycling triggered (Limit reached). Restarting...")
                    return # Exit loop to let Manager restart process
                
                # 3. Fetch Active Accounts (Round Robin)
                redis = await get_redis_client()
                active_accounts = await redis.smembers(ACTIVE_ACCOUNTS_KEY)
                
                if not active_accounts:
                    await asyncio.sleep(1.0)
                    continue
                
                # Convert set to list for iteration
                accounts_list = list(active_accounts)
                # Shuffle to avoid all workers hitting same account first? 
                # Or just iterate. Random is better for simple load balancing.
                import random
                random.shuffle(accounts_list)
                
                worked = False
                for acc_id_str in accounts_list:
                    if stop_event.is_set():
                        break
                        
                    account_id = int(acc_id_str)
                    
                    # Process ONE message (or batch) for this account
                    # We need a modified process function that doesn't loop forever
                    # Let's import the function but we need to modify it or call it in a way it returns.
                    # Currently `process_outgoing_queue` loops forever.
                    # We will modify `process_outgoing_queue` to accept `single_pass=True`
                    
                    processed_count = await process_outgoing_queue(
                        account_id=account_id, 
                        stop_event=stop_event, 
                        single_pass=True
                    )
                    
                    if processed_count > 0:
                        messages_processed += processed_count
                        worked = True
                        
                if not worked:
                    await asyncio.sleep(0.5) # Avoid busy loop if queues empty
                    
            except Exception as exc:
                local_logger.error("Worker loop error_type=%s", type(exc).__name__)
                await asyncio.sleep(1.0)
                
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        pass
    finally:
        local_logger.info(f"Worker Pool Process {worker_id} stopping.")

class WorkerPoolManager:
    def __init__(self, pool_size: int = None):
        # Default to CPU count or 4
        self.pool_size = pool_size or multiprocessing.cpu_count()
        self.workers = {} # worker_id -> Process
        self.stop_event = multiprocessing.Event()
        self.running = False

    async def start(self):
        self.running = True
        logger.info(f"Worker Pool Manager Started with {self.pool_size} workers.")
        
        # Initial Spawn
        for i in range(self.pool_size):
            self.spawn_worker(i)
            
        while self.running:
            try:
                # Monitor Health & Recycling
                for i in list(self.workers.keys()):
                    process = self.workers[i]
                    if not process.is_alive():
                        logger.warning(f"Worker {i} died/exited. Restarting...")
                        self.spawn_worker(i)
                
                # Check Heartbeats (Optional, but process.is_alive handles crash/exit)
                # If we want to detect frozen workers, we check redis heartbeats.
                # Implementation of heartbeat check:
                redis = await get_redis_client()
                for i in list(self.workers.keys()):
                    hb_key = f"worker:pool_{i}"
                    status = await redis.get(hb_key)
                    # If key missing (expired 60s), assume dead/frozen if process is alive?
                    # But process might be just starting.
                    # Let's rely on is_alive() + Recycling for now as per "Recycling" requirement.
                    pass

                await asyncio.sleep(5.0)
                
            except Exception as exc:
                logger.error("Manager loop failed error_type=%s", type(exc).__name__)
                await asyncio.sleep(5.0)

    def spawn_worker(self, worker_id: int):
        if worker_id in self.workers:
            p = self.workers[worker_id]
            if p.is_alive():
                p.terminate()
                
        p = multiprocessing.Process(
            target=run_worker_pool_process, 
            args=(worker_id, self.stop_event), 
            daemon=True
        )
        p.start()
        self.workers[worker_id] = p
        logger.info(f"Spawned Worker {worker_id} (PID: {p.pid})")

    def stop(self):
        logger.info("Worker Pool Manager Stopping...")
        self.running = False
        self.stop_event.set()
        
        for i, p in self.workers.items():
            if p.is_alive():
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
        logger.info("Worker Pool Manager Stopped.")

worker_pool = WorkerPoolManager(pool_size=settings.WORKER_POOL_SIZE)
