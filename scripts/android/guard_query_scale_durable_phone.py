"""Actual phone shell exit guard; no model execution or disconnect simulation."""
import argparse
from pathlib import Path
from phone_infer import Adb
from time_query_scale_production import durable_process, save, require

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--serial', required=True)
parser.add_argument('--record-dir', type=Path, required=True)
args = parser.parse_args()
directory = args.record_dir.resolve()
directory.mkdir(parents=True, exist_ok=False)
adb = Adb(args.serial)
device = adb.select()
require(device['hardware_serial'] == '1e7c759e', 'Wrong identity')
save(directory/'device.json', device)
remote = '/data/local/tmp/MNN/query-production-timer/'+directory.name
adb.shell('mkdir -p '+remote)
result = durable_process(adb, directory, remote, "sh -c 'echo RAW_GUARD; exit 7'")
require(result.stdout == b'RAW_GUARD\nDEVICE_TIMER_EXIT=7\n', 'Bad raw exit frame')
require((directory/'exit.code').read_bytes() == b'7\n', 'Bad real exit file')
save(directory/'audit.json', {'pass': True, 'matched_cases': 1, 'phone_shell_guard_executions': 1,
    'model_requests': 0, 'device_exit': 7, 'adb_commands': adb.commands})
print('DEVICE_GUARD_PASS exit7')
