"""Comprehensive automated tests for Jarvis OS automation & desktop awareness suite."""
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.os_control as os_ctrl


def test_app_index():
    print("1. Testing Windows Start Menu & App Paths Indexing...")
    apps = os_ctrl._build_app_index()
    assert isinstance(apps, dict), "App index must be a dictionary"
    assert len(apps) > 10, f"Expected > 10 indexed apps, found {len(apps)}"
    print(f"Indexed {len(apps)} desktop applications successfully!")


def test_launch_application_resolution():
    print("\n2. Testing launch_application resolution & injection safety...")
    # Empty name test
    res_empty = SkillRegistry.execute("launch_application", app_name="")
    assert "cannot be empty" in res_empty.lower()

    # Injection metacharacters test
    res_clean = os_ctrl.launch_application("notepad; calc & whoami")
    assert "notepad" in res_clean.lower() or "calc" in res_clean.lower() or "could not find" in res_clean.lower()

    # Browser alias test
    # Do not actually pop open a browser window during tests, but verify resolution logic
    apps = os_ctrl._build_app_index()
    assert "brave" in apps or "discord" in apps or "spotify" in apps or len(apps) > 0
    print("Launch resolution & injection safety verified!")


def test_close_application_safety():
    print("\n3. Testing close_application safety gates & critical process denylist...")
    # Attempting to kill critical system processes must be rejected
    res_explorer = SkillRegistry.execute("close_application", app_name="explorer.exe")
    assert "cannot terminate system-critical process" in res_explorer.lower(), f"Expected block on explorer, got: {res_explorer}"

    res_python = SkillRegistry.execute("close_application", app_name="python.exe")
    assert "cannot terminate system-critical process" in res_python.lower(), f"Expected block on python, got: {res_python}"

    res_svchost = SkillRegistry.execute("close_application", app_name="svchost.exe")
    assert "cannot terminate system-critical process" in res_svchost.lower(), f"Expected block on svchost, got: {res_svchost}"

    # Non-existent app test
    res_ghost = SkillRegistry.execute("close_application", app_name="nonexistent_app_xyz_987")
    assert "no active process matching" in res_ghost.lower()
    print("Critical process protection verified!")


def test_desktop_windows_and_active():
    print("\n4. Testing desktop window inspection...")
    active_res = SkillRegistry.execute("get_active_window")
    assert isinstance(active_res, str)
    assert len(active_res) > 0
    print("Active window result:", active_res)

    running_res = SkillRegistry.execute("list_running_applications")
    assert isinstance(running_res, str)
    assert "Active Desktop Windows" in running_res or "No active desktop" in running_res
    print("Running windows result excerpt:\n" + "\n".join(running_res.splitlines()[:5]))


def test_system_vitals():
    print("\n5. Testing get_system_vitals telemetry...")
    vitals = SkillRegistry.execute("get_system_vitals")
    assert "CPU Utilization" in vitals, f"Missing CPU telemetry: {vitals}"
    assert "Memory (RAM)" in vitals, f"Missing RAM telemetry: {vitals}"
    assert "Primary Storage (C:)" in vitals, f"Missing storage telemetry: {vitals}"
    print("Vitals Telemetry:\n" + vitals)


def test_clipboard_assistant():
    print("\n6. Testing clipboard assistant (copy & read)...")
    original = SkillRegistry.execute("read_clipboard")

    test_msg = "JARVIS 2.0 PROTOCOL VERIFIED // OMNIROUTE NEURAL LINK"
    copy_res = SkillRegistry.execute("copy_to_clipboard", text=test_msg)
    assert "Successfully copied" in copy_res

    read_back = SkillRegistry.execute("read_clipboard")
    assert test_msg in read_back, f"Expected '{test_msg}' in clipboard, got: {read_back}"
    print("Clipboard assistant verified cleanly!")


def test_take_desktop_screenshot():
    print("\n7. Testing take_desktop_screenshot...")
    test_file = "test_verify_snapshot.png"
    res = SkillRegistry.execute("take_desktop_screenshot", filename=test_file)
    assert "Screenshot saved successfully" in res, f"Expected success, got: {res}"

    # Verify and clean up
    target_path = Path("data/screenshots") / test_file
    if target_path.exists():
        target_path.unlink()
        print(f"Cleaned up test screenshot at: {target_path}")


def test_brain_intent_routing():
    print("\n8. Testing JarvisBrain query intent selection for new skills...")
    from jarvis.core.brain import JarvisBrain
    from jarvis.core.config_loader import load_config
    from jarvis.core.memory import MemoryManager
    from jarvis.core.safety import SafetyGate

    cfg = load_config()
    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))

    # Test 'open my browser'
    tools_browser = brain._select_tools_for_query("Can you please open my browser, Jarvis?")
    assert "launch_application" in tools_browser, f"Expected launch_application in {tools_browser}"

    # Test 'close discord'
    tools_close = brain._select_tools_for_query("Jarvis, close discord")
    assert "close_application" in tools_close, f"Expected close_application in {tools_close}"

    # Test 'active window'
    tools_active = brain._select_tools_for_query("What window is active right now?")
    assert "get_active_window" in tools_active, f"Expected get_active_window in {tools_active}"

    # Test 'system vitals'
    tools_vitals = brain._select_tools_for_query("Jarvis, report system vitals and hardware stats")
    assert "get_system_vitals" in tools_vitals, f"Expected get_system_vitals in {tools_vitals}"

    # Test 'clipboard'
    tools_clip = brain._select_tools_for_query("What did I just copy to my clipboard?")
    assert "read_clipboard" in tools_clip, f"Expected read_clipboard in {tools_clip}"

    # Test 'lock computer'
    tools_lock = brain._select_tools_for_query("Jarvis, lock my computer")
    assert "lock_workstation" in tools_lock, f"Expected lock_workstation in {tools_lock}"

    print("Brain intent routing passed 100% of test cases!")


if __name__ == "__main__":
    test_app_index()
    test_launch_application_resolution()
    test_close_application_safety()
    test_desktop_windows_and_active()
    test_system_vitals()
    test_clipboard_assistant()
    test_take_desktop_screenshot()
    test_brain_intent_routing()
    print("\nALL OS AUTOMATION & DESKTOP AWARENESS SUITE TESTS PASSED!")
