from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SH_WRAPPER = ROOT / "scripts" / "dgx_ssh.sh"
PS_WRAPPER = ROOT / "scripts" / "dgx_ssh.ps1"


def test_dgx_wrapper_is_fail_closed_and_does_not_store_credentials():
    text = SH_WRAPPER.read_text(encoding="utf-8")

    assert 'readonly DGX_HOST="140.96.58.171"' in text
    assert "StrictHostKeyChecking=yes" in text
    assert "ssh-copy-id" in text
    assert "-o StrictHostKeyChecking=yes" in text
    assert "BatchMode=yes" in text
    assert "ControlPersist=10m" in text
    assert "REAUTH_REQUIRED" in text
    assert "SSH_HOST_KEY_ERROR" in text
    assert "Host key verification failed" not in text
    assert "classify_ssh_failure" in text
    assert "return 75" in text
    assert "sshpass" not in text
    assert "expect" not in text
    assert "dangerously-skip-permissions" not in text
    assert "PRIVATE_KEY" not in text
    assert "PASSWORD" not in text


def test_windows_entrypoint_delegates_to_one_wsl_policy():
    text = PS_WRAPPER.read_text(encoding="utf-8")

    assert "dgx_ssh.sh" in text
    assert "wsl.exe -d Ubuntu -- bash" in text
    assert "System32\\OpenSSH\\ssh.exe" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "REAUTH_REQUIRED" in text
    assert "Get-SshFailureStatus" in text
    assert "host key verification failed" in text
    assert "if ($wslStatus -ne 75)" in text
    assert "both WSL and native Windows SSH routes failed" in text
    assert '"-o" "BatchMode=yes"' in text
    assert "wslOutput" in text
