#!/bin/bash
# Persistent RPyC bridge startup for MT5 container.
# Fixes numpy 2.x incompatibility and starts the rpyc SlaveService
# inside Wine's Python (where MetaTrader5 module lives).
#
# Bind-mount this into the container at /custom-cont-init.d/ or
# run via docker exec.

set -e

# Downgrade numpy if needed (MetaTrader5 5.0.36 requires numpy <2)
wine python -m pip install --no-cache-dir 'numpy<2' 2>/dev/null || true

# Start rpyc SlaveService in Wine Python on port 8001
exec wine python -c "
from rpyc.utils.server import ThreadedServer
from rpyc.core import SlaveService
import sys
print('Starting RPyC SlaveService on 0.0.0.0:8001...', flush=True)
t = ThreadedServer(SlaveService, hostname='0.0.0.0', port=8001, reuse_addr=True)
t.start()
"
