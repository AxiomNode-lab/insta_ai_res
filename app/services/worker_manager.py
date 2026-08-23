import asyncio
import logging
import multiprocessing
from multiprocessing import Process, Event
import time
import random
import signal
import sys
from app.services.instagram_service import process_outgoing_queue
from app.core.redis_utils import get_redis_client, set_worker_status, ACTIVE_ACCOUNTS_KEY, record_metric

logger = logging.getLogger(__name__)

# --- Worker Process Wrapper ---
def run_worker_process(account_id: int, stop_event: Event):
    """
    Isolated process entry point for a single account worker.
    """
    logging.basicConfig(level=logging.INFO, format=f'%(asctime)s [Worker-{account_id}] %(levelname)s: %(message)s')
    local_logger = logging.getLogger(f"worker_{account_id}")
    
    local_logger.info(f"Worker process started for Account {account_id}")
    
    async def worker_loop():
        # Staggered Startup Delay (0-120s)
        delay = random.uniform(0, 120.0)
        local_logger.info(f"Staggered startup: Waiting {delay:.2f}s...")
        for _ in range(int(delay * 10)):
            if stop_event.is_set():
                return
            await asyncio.sleep(0.1)
            
        local_logger.info("Worker active.")
        
        while not stop_event.is_set():
            try:
                start_time = time.time()
                
                # Heartbeat
                await set_worker_status(account_id, "alive")
                
                # Process
                await process_outgoing_queue(account_id=account_id, stop_event=stop_event)
                
                # Metrics: Processing Time (Average cycle time)
                duration = (time.time() - start_time) * 1000
                await record_metric("processing_time_ms", account_id, duration)
                
            except Exception as exc:
                local_logger.error("Worker loop failed error_type=%s", type(exc).__name__)
                await asyncio.sleep(5.0) 
                
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        pass
    finally:
        local_logger.info("Worker process shutting down.")

class WorkerManager:
    def __init__(self):
        self.workers = {} # account_id -> Process
        self.stop_event = multiprocessing.Event()
        self.running = False

    async def start(self):
        self.running = True
        logger.info("Worker Manager Started.")
        
        # Signal Handlers for Graceful Shutdown
        loop = asyncio.get_running_loop()
        # signal.signal can only be called in main thread, assume this is main thread context in lifespan
        # But asyncio handles signals via loop.add_signal_handler usually.
        # However, `worker_manager.stop()` is called in `lifespan` shutdown, so explicit signal handler here might be redundant if FastAPI handles SIGTERM.
        # We rely on `lifespan` calling `stop()`.
        
        while self.running:
            try:
                redis = await get_redis_client()
                active_ids = await redis.smembers(ACTIVE_ACCOUNTS_KEY)
                
                current_ids = set(self.workers.keys())
                target_ids = {int(i) for i in active_ids}
                
                # Spawn new
                for acc_id in target_ids - current_ids:
                    self.spawn_worker(acc_id)
                    
                # Monitor Health
                for acc_id, process in list(self.workers.items()):
                    if not process.is_alive():
                        logger.warning(f"Worker {acc_id} died! Restarting...")
                        self.spawn_worker(acc_id)
                        
                await asyncio.sleep(5.0)
                
            except Exception as exc:
                logger.error("Manager loop failed error_type=%s", type(exc).__name__)
                await asyncio.sleep(5.0)

    def spawn_worker(self, account_id: int):
        if account_id in self.workers:
            p = self.workers[account_id]
            if p.is_alive():
                p.terminate()
                
        p = Process(target=run_worker_process, args=(account_id, self.stop_event), daemon=True)
        p.start()
        self.workers[account_id] = p
        logger.info(f"Spawned worker for Account {account_id} (PID: {p.pid})")

    def stop(self):
        """
        Graceful Shutdown Logic.
        """
        logger.info("Worker Manager Stopping...")
        self.running = False
        self.stop_event.set() # Signal workers to stop
        
        # Wait for workers to finish (Graceful period)
        # We can join them with timeout
        for acc_id, p in self.workers.items():
            if p.is_alive():
                logger.info(f"Waiting for worker {acc_id} to finish...")
                p.join(timeout=5) # Wait 5s
                if p.is_alive():
                    logger.warning(f"Worker {acc_id} stuck, terminating...")
                    p.terminate()
                    
        logger.info("Worker Manager Stopped.")

worker_manager = WorkerManager()
