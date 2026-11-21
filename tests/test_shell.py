import pytest
from capri.tools.shell import run_bash, DANGEROUS_PATTERNS


@pytest.mark.asyncio
async def test_run_bash_safe():
    res = await run_bash("echo 'hello capri'")
    assert res == "hello capri"


@pytest.mark.asyncio
async def test_run_bash_empty():
    res = await run_bash("   ")
    assert "пустая команда" in res


@pytest.mark.asyncio
async def test_run_bash_dangerous_blocking():
    dangerous_cmds = [
        "rm -rf /",
        "rm -rf ~",
        "mkfs /dev/sda1",
        "dd if=/dev/zero of=/dev/nvme0n1",
        ":(){ :|:& };:",
        "chmod -R 777 /",
    ]
    for cmd in dangerous_cmds:
        res = await run_bash(cmd)
        assert "потенциально опасная или разрушительная" in res


@pytest.mark.asyncio
async def test_kill_process_protected():
    from unittest.mock import patch, mock_open
    from capri.tools.shell import kill_process
    res_vpn = await kill_process("happ")
    assert "заблокировано" in res_vpn

    res_xway = await kill_process("xwayland-satellite")
    assert "заблокировано" in res_xway

    # Тестируем усечённый /proc/<pid>/comm (xwayland-satell)
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data="xwayland-satell\n")):
        res_pid = await kill_process("99999")
        assert "заблокировано" in res_pid


@pytest.mark.asyncio
async def test_kill_process_niri_close_window():
    from unittest.mock import patch, AsyncMock
    from capri.tools.shell import kill_process

    mock_niri_windows = b'[{"id": 42, "app_id": "Chatgpt", "title": "ChatGPT", "pid": 1437}]'

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        # Mock niri msg -j windows
        proc1 = AsyncMock()
        proc1.communicate.return_value = (mock_niri_windows, b"")
        proc1.returncode = 0

        # Mock niri msg action close-window --id 42
        proc2 = AsyncMock()
        proc2.communicate.return_value = (b"", b"")
        proc2.returncode = 0

        mock_exec.side_effect = [proc1, proc2]

        res = await kill_process("chatgpt")
        assert "ID 42" in res
        assert "успешно закрыто через Niri" in res

        # Verify it called close-window and NOT kill -15 1437!
        mock_exec.assert_any_call(
            "niri", "msg", "action", "close-window", "--id", "42",
            stdout=-1, stderr=-1
        )


@pytest.mark.asyncio
async def test_kill_process_never_kills_self_or_parent():
    from unittest.mock import patch, AsyncMock
    from capri.tools.shell import kill_process
    import os

    # If pgrep returns own PID and parent PID
    mock_niri = AsyncMock()
    mock_niri.communicate.return_value = (b"[]", b"")
    mock_niri.returncode = 0

    mock_pgrep = AsyncMock()
    mock_pgrep.communicate.return_value = (f"{os.getpid()}\n{os.getppid()}\n".encode(), b"")
    mock_pgrep.returncode = 0

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [mock_niri, mock_pgrep]
        res = await kill_process("capri_worker")
        assert "не найден" in res
        # Ensure 'kill -15' was NEVER called on self or parent
        for call_item in mock_exec.call_args_list:
            assert call_item[0][0] != "kill"


