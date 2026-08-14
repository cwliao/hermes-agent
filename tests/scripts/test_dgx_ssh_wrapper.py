import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SH_WRAPPER = ROOT / "scripts" / "dgx_ssh.sh"
PS_WRAPPER = ROOT / "scripts" / "dgx_ssh.ps1"


def run_shell_wrapper(tmp_path: Path, mode: str, *args: str) -> subprocess.CompletedProcess[str]:
    wsl = shutil.which("wsl.exe")
    bash = shutil.which("bash")
    if wsl is None and bash is None:
        import pytest

        pytest.skip("bash or wsl.exe is required for wrapper behavior tests")

    fake_ssh = tmp_path / "ssh"
    fake_ssh_content = """#!/usr/bin/env bash
set -u
if [[ -n "${FAKE_SSH_LOG:-}" ]]; then
    printf '<%s>\\n' "$@" >> "${FAKE_SSH_LOG}"
fi
case "${FAKE_SSH_MODE:-success}" in
    permission)
        printf '%s\\n' 'Permission denied (publickey).' >&2
        exit 255
        ;;
    hostkey)
        printf '%s\\n' 'Host key verification failed.' >&2
        exit 255
        ;;
    success)
        if [[ "${@: -1}" == "true" ]]; then
            exit 0
        fi
        printf '%s\\n' 'SSH_OK' 'dgx-test' 'cwliao'
        exit 0
        ;;
    *)
        exit 99
        ;;
esac
"""
    with fake_ssh.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(fake_ssh_content)
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    log_path = tmp_path / "ssh-args.log"
    if wsl is not None:
        def wslpath(path: Path) -> str:
            converted = subprocess.run(
                [wsl, "-d", "Ubuntu", "--", "wslpath", "-a", str(path).replace("\\", "/")],
                text=True,
                capture_output=True,
                check=False,
            )
            assert converted.returncode == 0, converted.stderr
            return converted.stdout.strip()

        wrapper_path = wslpath(SH_WRAPPER)
        fake_path = wslpath(fake_ssh)
        wsl_tmp = wslpath(tmp_path)
        wsl_log = wslpath(log_path)
        subprocess.run([wsl, "-d", "Ubuntu", "--", "chmod", "+x", fake_path], check=True)
        wsl_env = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        return subprocess.run(
            [
                wsl,
                "-d",
                "Ubuntu",
                "--",
                "env",
                f"PATH={wsl_tmp}:{wsl_env}",
                f"HOME={wsl_tmp}",
                f"XDG_RUNTIME_DIR={wsl_tmp}",
                f"FAKE_SSH_MODE={mode}",
                f"FAKE_SSH_LOG={wsl_log}",
                "bash",
                wrapper_path,
                *args,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(tmp_path)
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    env["FAKE_SSH_MODE"] = mode
    env["FAKE_SSH_LOG"] = str(log_path)
    return subprocess.run(
        [bash, str(SH_WRAPPER).replace("\\", "/"), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


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
    assert "BOOTSTRAP_KEYGEN_FAILED" in text
    assert "return 70" in text
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
    assert "HERMES_DGX_IDENTITY" in text
    assert "Invoke-NativeProbe" in text
    assert ".Status" in text
    assert ".Output" in text
    assert "BLOCKED_WSL_UNAVAILABLE" in text


def test_shell_probe_classifies_permission_denied_as_reauthentication(tmp_path):
    result = run_shell_wrapper(tmp_path, "permission", "probe")

    assert result.returncode == 75
    assert "REAUTH_REQUIRED" in result.stderr


def test_shell_probe_preserves_host_key_failure_classification(tmp_path):
    result = run_shell_wrapper(tmp_path, "hostkey", "probe")

    assert result.returncode == 255
    assert "SSH_HOST_KEY_ERROR" in result.stderr
    assert "REAUTH_REQUIRED" not in result.stderr


def test_shell_exec_forwards_remote_command_arguments(tmp_path):
    result = run_shell_wrapper(tmp_path, "success", "exec", "echo", "hello")
    log = (tmp_path / "ssh-args.log").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "<echo>" in log
    assert "<hello>" in log
