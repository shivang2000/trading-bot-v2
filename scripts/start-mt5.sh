#!/bin/bash

# Patched MT5 startup script for gmag11/metatrader5_vnc container.
# Fixes step 7: mt5linux 1.0.3 removed the -w flag, and numpy 2.x
# broke MetaTrader5 imports. This version starts rpyc inside Wine's
# Python directly.

# Configuration variables
mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
WINEPREFIX='/config/.wine'
WINEDEBUG='-all'
wine_executable="wine"
metatrader_version="5.0.36"
mt5server_port="8001"
MT5_CMD_OPTIONS="${MT5_CMD_OPTIONS:-}"
mono_url="https://dl.winehq.org/wine/wine-mono/10.3.0/wine-mono-10.3.0-x86.msi"
python_url="https://www.python.org/ftp/python/3.9.13/python-3.9.13.exe"
mt5setup_url="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

show_message() {
    echo $1
}

check_dependency() {
    if ! command -v $1 &> /dev/null; then
        echo "$1 is not installed. Please install it to continue."
        exit 1
    fi
}

is_python_package_installed() {
    python3 -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
    return $?
}

is_wine_python_package_installed() {
    $wine_executable python -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
    return $?
}

check_dependency "curl"
check_dependency "$wine_executable"

# Install Mono if not present
if [ ! -e "/config/.wine/drive_c/windows/mono" ]; then
    show_message "[1/7] Downloading and installing Mono..."
    curl -o /config/.wine/drive_c/mono.msi $mono_url
    WINEDLLOVERRIDES=mscoree=d $wine_executable msiexec /i /config/.wine/drive_c/mono.msi /qn
    rm /config/.wine/drive_c/mono.msi
    show_message "[1/7] Mono installed."
else
    show_message "[1/7] Mono is already installed."
fi

# Check if MetaTrader 5 is already installed
if [ -e "$mt5file" ]; then
    show_message "[2/7] File $mt5file already exists."
else
    show_message "[2/7] File $mt5file is not installed. Installing..."
    $wine_executable reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    show_message "[3/7] Downloading MT5 installer..."
    curl -o /config/.wine/drive_c/mt5setup.exe $mt5setup_url
    show_message "[3/7] Installing MetaTrader 5..."
    $wine_executable "/config/.wine/drive_c/mt5setup.exe" "/auto" &
    wait
    rm -f /config/.wine/drive_c/mt5setup.exe
fi

# Run MT5
if [ -e "$mt5file" ]; then
    show_message "[4/7] File $mt5file is installed. Running MT5..."
    $wine_executable "$mt5file" $MT5_CMD_OPTIONS &
else
    show_message "[4/7] File $mt5file is not installed. MT5 cannot be run."
fi

# Install Python in Wine if not present
if ! $wine_executable python --version 2>/dev/null; then
    show_message "[5/7] Installing Python in Wine..."
    curl -L $python_url -o /tmp/python-installer.exe
    $wine_executable /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    rm /tmp/python-installer.exe
    show_message "[5/7] Python installed in Wine."
else
    show_message "[5/7] Python is already installed in Wine."
fi

# Upgrade pip and install required packages
show_message "[6/7] Installing Python libraries"
$wine_executable python -m pip install --upgrade --no-cache-dir pip
show_message "[6/7] Installing MetaTrader5 library in Windows"
if ! is_wine_python_package_installed "MetaTrader5==$metatrader_version"; then
    $wine_executable python -m pip install --no-cache-dir MetaTrader5==$metatrader_version
fi
show_message "[6/7] Checking and installing mt5linux library in Windows if necessary"
if ! is_wine_python_package_installed "mt5linux"; then
    $wine_executable python -m pip install --no-cache-dir "mt5linux>=0.1.9"
fi
if ! is_wine_python_package_installed "python-dateutil"; then
    show_message "[6/7] Installing python-dateutil library in Windows"
    $wine_executable python -m pip install --no-cache-dir python-dateutil
fi

show_message "[6/7] Checking and installing mt5linux library in Linux if necessary"
if ! is_python_package_installed "mt5linux"; then
    pip install --break-system-packages --no-cache-dir --no-deps mt5linux && \
    pip install --break-system-packages --no-cache-dir rpyc plumbum numpy
fi
show_message "[6/7] Checking and installing pyxdg library in Linux if necessary"
if ! is_python_package_installed "pyxdg"; then
    pip install --break-system-packages --no-cache-dir pyxdg
fi

# ─── PATCHED STEP 7 ───────────────────────────────────────
# Fix: numpy 2.x breaks MetaTrader5 imports; downgrade in Wine
show_message "[7/7] Fixing numpy and starting RPyC server..."
$wine_executable python -m pip install --no-cache-dir 'numpy<2' 2>/dev/null

# Start rpyc SlaveService directly in Wine Python (where MetaTrader5 lives)
$wine_executable python -c "
from rpyc.utils.server import ThreadedServer
from rpyc.core import SlaveService
import sys
print('[7/7] RPyC SlaveService starting on 0.0.0.0:${mt5server_port}...', flush=True)
sys.stdout.flush()
t = ThreadedServer(SlaveService, hostname='0.0.0.0', port=${mt5server_port}, reuse_addr=True)
t.start()
" &

sleep 5

if ss -tuln | grep ":$mt5server_port" > /dev/null; then
    show_message "[7/7] RPyC server is running on port $mt5server_port."
else
    show_message "[7/7] Failed to start RPyC server on port $mt5server_port."
fi
