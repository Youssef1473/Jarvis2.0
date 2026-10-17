"""Test for Jarvis HUD lifecycle and state transitions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from jarvis.ui.hud import JarvisHUD


def test_hud():
    hud = JarvisHUD()
    print("Starting HUD in background...")
    hud.start()
    time.sleep(0.5)

    print("Testing LISTENING state...")
    hud.set_state("listening")
    time.sleep(0.3)

    print("Testing THINKING state...")
    hud.set_state("thinking")
    time.sleep(0.3)

    print("Testing SPEAKING state...")
    hud.set_state("speaking")
    time.sleep(0.3)

    print("Stopping HUD...")
    hud.stop()
    print("HUD test completed successfully!")


if __name__ == "__main__":
    test_hud()
