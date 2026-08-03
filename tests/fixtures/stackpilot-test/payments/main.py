import os
import time

# Sleep so ProcessManager can observe a live process during tests.
# Tests set STACKPILOT_SLEEP to a small value for fast teardown.
time.sleep(float(os.environ.get('STACKPILOT_SLEEP', '3600')))
