"""Dangerous commands are classifier inputs only; no interpreter or shell runs."""
import pytest

from excudo.permissions import ActionRisk, PermissionGate
from excudo.security import blocked_command_reason


ADDITIONAL_CLI_CASES = [
    pytest.param('dd if=/dev/zero of=/dev/sda bs=1M count=1', id='raw-disk-write'),
    pytest.param('dd if=image.img > /dev/nvme0n1', id='raw-disk-redirection'),
    pytest.param(':(){ :|:& };:', id='fork-bomb'),
    pytest.param('function bomb { bomb | bomb & }; bomb', id='named-fork-bomb'),
]


@pytest.mark.parametrize('command', ADDITIONAL_CLI_CASES + [
    'dd of="/dev/disk0" if=image.img',
    '/bin/dd if=image.img of=/dev/rdisk0',
    'sudo dd if=image.img of=/dev/mapper/root',
    "dd if=image.img of='\\\\.\\PhysicalDrive0'",
    "dd if=image.img of='\\\\.\\C:'",
    'dd if=image.img >> /dev/vda',
    'dd if=image.img 1> /dev/sdb',
    'echo before && dd if=image.img of=/dev/sda',
    'f () { f|f& }; f',
    'f(){ f & f & }; f',
    'f(){ f|f; }; f',
])
def test_explicit_catastrophic_forms_are_automatically_refused(command, tmp_path):
    assert blocked_command_reason(command)
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True,
        approval_callback=lambda *_: pytest.fail('Catastrophic command reached approval'))
    result = gate.check('bash_exec', {'command': command}, ActionRisk.EXECUTE)
    assert not result.allowed and result.risk == ActionRisk.BLOCKED
    assert result.reason.startswith('Blocked dangerous command (')


@pytest.mark.parametrize('command', [
    'dd if=/dev/sda of=./backup.img count=1',
    'dd if=/dev/zero of=./disk.img count=1',
    'dd if=./file of=/dev/null',
    'dd if=./file of=/dev/shm/buffer',
    'dd if=./file of=/dev/fd/1',
    'dd if=image.img of=./dev/sda',
    "dd if=image.img of=./backup.img; printf '/dev/sda'",
    "f(){ printf 'ordinary function\\n'; }; f",
    'f(){ false && f; }; f',
    'first(){ second & }; first',
    "python3 -c \"print('normal code')\"",
    'printf hello',
])
def test_regular_commands_still_follow_normal_approval(command):
    assert blocked_command_reason(command) is None
