"""Always-on catastrophic-command guard in exec_command (accidental-wipe
protection). Conservative: blocks unambiguous system-destroyers, never legit
recursive deletes of project subdirs."""
import os

from tools import exec_command as E

DANGEROUS = [
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf $HOME", "sudo rm -rf /",
    "rm -fr /etc", "rm --recursive --force /", "rm -rf /usr", "rm -rf /home",
    "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sdb", "mkfs /dev/nvme0n1",
    ":(){ :|:& };:", "echo x > /dev/sda", "chmod -R 000 /", "chown -R root /",
    "wipefs -a /dev/sdb", "cd /tmp && rm -rf /", "FOO=1 rm -rf ~",
]

SAFE = [
    "rm -rf /tmp/foo", "rm -rf ./build", "rm -rf node_modules", "rm -f a.txt",
    "rm -rf /home/mike/project/dist", "rm -rf dist", "rm -ri /tmp/x",
    "dd if=input.bin of=output.img bs=1M", "git commit -m 'rm -rf / joke'",
    "chmod +x run.sh", "chmod -R 755 ./mydir", "echo hello > out.txt",
    "python3 -m pytest", "grep -rf pattern .", "mkfs.ext4 disk.img",
    "ls -la /", "cat /etc/hostname", "find / -name foo 2>/dev/null",
]


def test_dangerous_all_flagged():
    missed = [c for c in DANGEROUS if E._is_catastrophic(c) is None]
    assert not missed, f"catastrophic commands slipped through: {missed}"


def test_safe_all_allowed():
    fp = [(c, E._is_catastrophic(c)) for c in SAFE if E._is_catastrophic(c)]
    assert not fp, f"false positives on safe commands: {fp}"


def test_fn_blocks_dangerous():
    # Flagged form that is HARMLESS even if the guard failed (device doesn't
    # exist → redirect just errors), so the test can't damage the machine.
    out = E.fn(command="echo x > /dev/sdznonexistentdevice")
    assert out.startswith("Error: BLOCKED") and "catastrophic" in out


def test_fn_allows_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = E.fn(command="echo hi > out.txt")
    assert "BLOCKED" not in out
    assert (tmp_path / "out.txt").exists()


def test_env_override_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config here → env is the only signal
    monkeypatch.delenv("AGENT_ALLOW_CATASTROPHIC", raising=False)
    assert E._catastrophic_allowed() is False
    monkeypatch.setenv("AGENT_ALLOW_CATASTROPHIC", "1")
    assert E._catastrophic_allowed() is True


def test_config_override_flag(tmp_path, monkeypatch):
    import json
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ALLOW_CATASTROPHIC", raising=False)
    (tmp_path / ".agent").mkdir()
    with open(tmp_path / ".agent" / "config.json", "w") as f:
        json.dump({"allow_catastrophic_commands": True}, f)
    assert E._catastrophic_allowed() is True
