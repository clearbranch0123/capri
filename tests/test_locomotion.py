import pytest
from unittest.mock import patch, MagicMock
from PySide6.QtWidgets import QApplication

# Ensure QApplication exists for QObject and Signals
app = QApplication.instance() or QApplication([])

from capri.ui.locomotion import LocomotionMode, LocomotionController, DesktopObserver, DEFAULT_HOME_COORDS
from capri.ui.client import notify_avatar_locomotion
from capri.tools.desktop import capri_locomotion


def test_locomotion_constants():
    assert LocomotionMode.STAY == "stay"
    assert LocomotionMode.FOLLOW == "follow"
    assert DEFAULT_HOME_COORDS == (1590.0, 615.0)


def test_desktop_observer_fullscreen():
    observer = DesktopObserver()
    fs_signals = []
    observer.fullscreen_changed.connect(lambda val: fs_signals.append(val))

    # Regular window
    normal_win = {
        "id": 1,
        "layout": {"window_size": [1870, 985], "tile_size": [1870, 985]}
    }
    observer._check_fullscreen(normal_win)
    assert observer.is_fullscreen is False
    assert len(fs_signals) == 0

    # Fullscreen window
    fs_win = {
        "id": 2,
        "layout": {"window_size": [1920, 1080], "tile_size": [1920, 1080]}
    }
    observer._check_fullscreen(fs_win)
    assert observer.is_fullscreen is True
    assert fs_signals == [True]

    # Back to normal
    observer._check_fullscreen(normal_win)
    assert observer.is_fullscreen is False
    assert fs_signals == [True, False]


def test_locomotion_controller_modes():
    controller = LocomotionController()

    assert controller.mode == LocomotionMode.STAY

    # Follow mode
    captions = []
    controller.caption_requested.connect(lambda txt: captions.append(txt))

    ok, msg = controller.follow(True)
    assert ok is True
    assert controller.mode == LocomotionMode.FOLLOW
    assert any("следования" in c for c in captions)

    # Stay mode
    ok, msg = controller.stay()
    assert ok is True
    assert controller.mode == LocomotionMode.STAY
    assert any("Остаюсь" in c for c in captions)


def test_locomotion_handle_command():
    controller = LocomotionController()

    with patch.object(controller, "come", return_value=(True, "come")) as mock_come, \
         patch.object(controller, "stay", return_value=(True, "stay")) as mock_stay, \
         patch.object(controller, "follow", return_value=(True, "follow")) as mock_follow, \
         patch.object(controller, "home", return_value=(True, "home")) as mock_home:

        ok, _ = controller.handle_command({"action": "come"})
        assert ok is True
        mock_come.assert_called_once()

        ok, _ = controller.handle_command({"action": "stay"})
        assert ok is True
        mock_stay.assert_called_once()

        ok, _ = controller.handle_command({"action": "follow", "enabled": True})
        assert ok is True
        mock_follow.assert_called_once_with(enabled=True)

        ok, _ = controller.handle_command({"action": "home"})
        assert ok is True
        mock_home.assert_called_once()


@pytest.mark.asyncio
async def test_capri_locomotion_tool():
    with patch("capri.ui.client.notify_avatar_locomotion", return_value=True) as mock_notify:
        res = await capri_locomotion("come")
        assert "КО МНЕ" in res
        mock_notify.assert_called_with("come")

        res = await capri_locomotion("stay")
        assert "Stay Mode" in res
        mock_notify.assert_called_with("stay")

        res = await capri_locomotion("follow", "on")
        assert "включено" in res
        mock_notify.assert_called_with("follow", enabled=True)

        res = await capri_locomotion("home")
        assert "Home Base" in res
        mock_notify.assert_called_with("home")


def test_notify_avatar_locomotion_no_socket():
    # When socket does not exist, it safely returns False without crashing
    with patch("pathlib.Path.exists", return_value=False):
        assert notify_avatar_locomotion("come", retries=1) is False


def test_home_bounds_clamping():
    controller = LocomotionController()
    # Mock smaller screen, e.g. 1366x768 laptop screen and starting coordinates far away
    with patch.object(controller.observer, "get_screen_size", return_value=(1366, 768)), \
         patch.object(controller, "get_current_coords", return_value=(0.0, 0.0)):
        with patch.object(controller, "teleport") as mock_teleport:
            controller.home()
            mock_teleport.assert_called_once()
            target_x, target_y = mock_teleport.call_args[0]
            # Avatar size 300x420, so target_x <= 1366 - 300 - 30 = 1036
            # target_y <= 768 - 420 - 45 = 303
            assert target_x <= 1036.0
            assert target_y <= 303.0


def test_finish_teleport_preserves_fullscreen_opacity():
    controller = LocomotionController()
    opacity_events = []
    controller.opacity_requested.connect(lambda op: opacity_events.append(op))

    # In fullscreen mode, opacity should be restored to 0.18, not 1.0
    controller.observer.is_fullscreen = True
    controller._finish_teleport(controller._teleport_generation, 100.0, 100.0)
    assert opacity_events[-1] == 0.18

    # When not in fullscreen, opacity should be 1.0
    controller.observer.is_fullscreen = False
    controller._finish_teleport(controller._teleport_generation, 100.0, 100.0)
    assert opacity_events[-1] == 1.0


def test_stale_teleport_callback_cancelled():
    controller = LocomotionController()
    old_gen = controller._teleport_generation
    controller._teleport_generation += 1  # Simulated arrival of a newer command

    finished_events = []
    controller.locomotion_finished.connect(lambda act, x, y: finished_events.append((act, x, y)))

    # Calling with old_gen should be a no-op
    controller._finish_teleport(old_gen, 50.0, 50.0)
    assert len(finished_events) == 0

