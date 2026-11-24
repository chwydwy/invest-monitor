"""Receive a DPAPI helper environment variable; transfer via encrypted SSH stdin."""
import os
import shlex
import subprocess


if __name__=='__main__':
    token=os.environ.pop('OPERATIONS_REPORT_TOKEN','')
    if len(token)<32 or any(c.isspace() for c in token):raise SystemExit('Protected status-report credential required')
    program='''import os,sys
from pathlib import Path
base=Path('/home/pi5dart/.config/invest-monitor')
assert base.is_dir() and base.stat().st_uid==os.getuid()
assert base.stat().st_mode & 0o077 == 0
data=sys.stdin.buffer.read(4097)
assert 32<=len(data)<=4096 and not any(chr(c).isspace() for c in data)
target=base/'operations-report.token'
fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'wb') as handle:handle.write(data)
print('operations token installed')
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','pi5','python3 -c '+shlex.quote(program)],
                          input=token.encode('utf-8'),capture_output=True)
    if result.returncode:raise SystemExit('Protected status token installation failed; existing files were preserved')
    print('operations token installed')
