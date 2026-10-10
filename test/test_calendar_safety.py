"""Exercise malformed calendar values and serialization boundaries under sanitizers."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_calendar_boundaries_and_malformed_alarm(tmp_path):
    compiler = shutil.which('c++')
    if os.name == 'nt' or not compiler:
        pytest.skip('This sanitizer check needs a Unix C++ compiler; Windows builds compile the same library with MSVC.')
    root = Path(__file__).resolve().parents[1]
    vendor = root / 'Vendor' / 'icalendarlib'
    binary = tmp_path / 'calendar-safety'
    subprocess.run([compiler, '-std=c++17', '-fsanitize=address,undefined', '-g', '-I', str(vendor),
                    str(root / 'test' / 'calendar_safety.cpp'), str(vendor / 'date.cpp'),
                    str(vendor / 'types.cpp'), '-o', str(binary)], check=True, capture_output=True, timeout=60)
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
