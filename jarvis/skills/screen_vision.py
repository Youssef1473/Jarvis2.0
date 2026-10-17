"""Screen inspection and visual perception skill for Jarvis 2.0.

Provides multimodal visual perception of Sir's workstation screen using
ScreenPerception desktop capture, automated Visual Privacy Guard protection,
and Google Gemini Multimodal Vision, with instant offline Semantic UIA fallback.
"""
import io
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from jarvis.skills.base import SkillRegistry
from jarvis.vision.screen_capture import ScreenPerception

logger = logging.getLogger("Jarvis.Skills.Vision")

# Priority cascade for cloud vision inference
VISION_CANDIDATE_MODELS = [
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-3.8-flash",
]

SENSITIVE_PROCESS_NAMES = {
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe",
    "dashlane.exe", "authenticator.exe", "metamask.exe", "ledger live.exe",
    "trezor suite.exe", "windowsecurity.exe", "credentialuibroker.exe", "authy.exe"
}

SENSITIVE_EXPLICIT_KEYWORDS = {
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "kdbx",
    "authenticator", "online banking", "netbanking", "bank of america",
    "chase online", "wells fargo", "citibank", "hsbc", "barclays", "schwab",
    "fidelity", "metamask", "phantom wallet", "trust wallet", "ledger live",
    "trezor", "private key", "seed phrase", "master password", "credential manager",
    "sign in - google accounts"
}

DEV_PROCESS_NAMES = {
    "code.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
    "sublime_text.exe", "notepad++.exe", "cursor.exe"
}

LOGIN_PATTERN = re.compile(r"\b(log\s*in|sign\s*in|sign\s*on|enter\s*password)\b", re.IGNORECASE)


def _get_active_window_details() -> Tuple[str, str]:
    """Returns (window_title, process_name) for the currently focused foreground window."""
    try:
        import win32gui
        import win32process
        import psutil
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ("", "")
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = ""
        try:
            proc_name = psutil.Process(pid).name()
        except Exception:
            pass
        return (title, proc_name)
    except Exception as e:
        logger.debug("Failed to get active window details: %s", e)
        return ("", "")


def check_visual_privacy(window_title: str, process_name: str = "") -> Optional[str]:
    """Inspects window title and process against sensitive privacy profiles.
    Returns a security warning string if sensitive, or None if safe for capture.
    """
    title_lower = (window_title or "").lower()
    proc_lower = (process_name or "").lower()

    is_sensitive = False
    if proc_lower in SENSITIVE_PROCESS_NAMES:
        is_sensitive = True
    elif any(kw in title_lower for kw in SENSITIVE_EXPLICIT_KEYWORDS):
        is_sensitive = True
    elif LOGIN_PATTERN.search(title_lower) and proc_lower not in DEV_PROCESS_NAMES:
        is_sensitive = True

    if is_sensitive:
        target_name = window_title if window_title else process_name
        return (
            f"Visual capture paused for your security, Sir. The active window appears to contain "
            f"sensitive credentials or financial information ('{target_name}'). "
            f"I have refrained from capturing or processing the screen."
        )
    return None


def _broadcast_screenshot_event():
    """Notifies the Holographic HUD that a new screen capture is available."""
    try:
        from jarvis.core.task_manager import BackgroundTaskManager
        mgr = BackgroundTaskManager.get_instance()
        if mgr.orchestrator and hasattr(mgr.orchestrator, "ui_service"):
            ui = mgr.orchestrator.ui_service
            if ui:
                import time
                ui.broadcast({
                    "type": "screen_snapshot",
                    "url": f"/api/screenshot/latest?t={int(time.time() * 1000)}",
                    "timestamp": time.strftime("%H:%M:%S"),
                })
    except Exception as e:
        logger.debug("Failed to broadcast screenshot event: %s", e)


def _save_latest_screenshot(image_bytes: bytes) -> Optional[Path]:
    """Caches the captured screen frame to data/screenshots/latest_screen.jpg with path confinement."""
    try:
        from jarvis.core.security_boundaries import validate_confined_path
        data_dir = os.path.abspath("data")
        save_dir = os.path.join(data_dir, "screenshots")
        os.makedirs(save_dir, exist_ok=True)
        raw_target = os.path.join(save_dir, "latest_screen.jpg")
        safe_target = validate_confined_path(raw_target, data_dir)
        target = Path(safe_target)
        target.write_bytes(image_bytes)
        _broadcast_screenshot_event()
        return target
    except Exception as e:
        logger.debug("Could not cache latest screenshot: %s", e)
        return None


def _analyze_screen_with_gemini(image_bytes: bytes, query: str) -> Optional[str]:
    """Sends screen image bytes to Gemini Multimodal Vision API with deadline enforcement."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not configured for visual perception.")
        return None

    try:
        from google import genai
        from google.genai import types

        # Minimum deadline for google.genai is 10s (10000ms); set 12000ms for safety
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=12000))
        prompt = (
            "You are JARVIS's visual cortex. Describe what is currently displayed on Sir's workstation screen "
            "with clarity, technical precision, and British composure. "
            "Identify active applications, open windows, text, code, browser tabs, or media. "
            f"Address Sir's focus: '{query}'."
        )

        for model_name in VISION_CANDIDATE_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        prompt,
                    ],
                )
                if response and response.text:
                    logger.info("Screen vision successfully analyzed via model '%s'.", model_name)
                    return response.text.strip()
            except Exception as model_err:
                logger.debug("Vision model '%s' failed (%s). Cascading to next candidate...", model_name, model_err)

    except Exception as err:
        logger.error("Error connecting to Gemini Vision API: %s", err)

    return None


@SkillRegistry.register(
    name="inspect_screen",
    description="Captures and visually analyzes Sir's active workstation screen to inspect open apps, errors, documents, or content."
)
def inspect_screen(query: str = "Describe what is currently visible on the screen") -> str:
    """Captures and inspects Sir's desktop display with multimodal vision."""
    logger.info("Screen inspection requested with query: %s", query)

    # 1. Visual Privacy Guard: check active foreground window before capturing
    active_title, active_proc = _get_active_window_details()
    privacy_notice = check_visual_privacy(active_title, active_proc)
    if privacy_notice:
        logger.warning(
            "Visual Privacy Guard triggered for window '%s' (%s). Refraining from capture.",
            active_title, active_proc
        )
        return privacy_notice

    # 2. Capture screen
    image_bytes = ScreenPerception.capture_screen_bytes(max_width=1280, quality=75)
    if not image_bytes:
        return "I was unable to capture your display, Sir. Please ensure desktop permissions and interactive session access are granted."

    # Cache latest captured frame locally for reference and auditing
    _save_latest_screenshot(image_bytes)

    # 3. Analyze via Gemini Multimodal Vision (Tier 1 Cloud Vision)
    analysis = _analyze_screen_with_gemini(image_bytes, query)
    if analysis:
        return analysis

    # 4. Graceful fallback using active OS window telemetry & UI Automation controls (Tier 2 Semantic UIA)
    try:
        from jarvis.skills.os_control import get_active_window, list_running_applications
        active = get_active_window()
        apps = list_running_applications()

        controls_summary = ""
        try:
            from jarvis.skills.windows_uia import inspect_window_controls
            if active_title:
                uia_tree = inspect_window_controls(active_title, max_depth=2)
                if uia_tree and "No interactive child elements found" not in uia_tree:
                    controls_summary = f"\n\n{uia_tree}"
        except Exception as uia_err:
            logger.debug("UIA controls inspection skipped during fallback: %s", uia_err)

        return (
            f"Visual cloud inference was momentarily unavailable, Sir. However, according to desktop telemetry:\n"
            f"{active}\n\n"
            f"{apps}"
            f"{controls_summary}"
        )
    except Exception:
        return "I captured your screen display, Sir, but encountered a momentary delay in visual processing."


@SkillRegistry.register(
    name="see_screen",
    description="Visually perceives Sir's desktop screen in real-time, describing open applications, code, browser tabs, or text."
)
def see_screen(focus: str = "Describe what is visible on the screen") -> str:
    """Convenience alias for screen perception."""
    return inspect_screen(query=focus)


@SkillRegistry.register(
    name="inspect_screen_with_marks",
    description="Captures the active window and visually annotates interactive UI elements with Set-of-Mark numbered badges ([1], [2], etc.) for deterministic visual grounding and clicking."
)
def inspect_screen_with_marks(query: str = "Identify interactive elements on the screen") -> str:
    """Captures and annotates the active window with Set-of-Mark badges."""
    logger.info("inspect_screen_with_marks requested with query: %s", query)

    # 1. Visual Privacy Guard: check active foreground window before capturing
    active_title, active_proc = _get_active_window_details()
    privacy_notice = check_visual_privacy(active_title, active_proc)
    if privacy_notice:
        logger.warning(
            "Visual Privacy Guard triggered for window '%s' (%s). Refraining from mark inspection.",
            active_title, active_proc
        )
        return privacy_notice

    # 2. Extract and annotate UIA elements
    from jarvis.vision.som_grounding import SetOfMarkAnnotator
    annotator = SetOfMarkAnnotator(max_controls=75, min_size=12)
    annotated_img, marks, manifest = annotator.annotate_window()

    if annotated_img is None or not marks:
        return f"Unable to annotate interactive controls in '{active_title}', Sir: {manifest}"

    # Cache annotated screenshot locally
    buf = io.BytesIO()
    annotated_img.save(buf, format="JPEG", quality=80)
    img_bytes = buf.getvalue()
    _save_latest_screenshot(img_bytes)

    som_target_dir = os.path.abspath("data/screenshots")
    try:
        from jarvis.core.security_boundaries import validate_confined_path
        safe_som_path = validate_confined_path(os.path.join(som_target_dir, "latest_som_screen.jpg"), som_target_dir)
        os.makedirs(os.path.dirname(safe_som_path), exist_ok=True)
        annotated_img.save(safe_som_path, format="JPEG", quality=85)
    except Exception as som_err:
        logger.debug("Saving latest_som_screen.jpg notice: %s", som_err)

    # 3. Analyze via Gemini Multimodal Vision if available
    analysis_prompt = (
        f"{query}\n\n"
        f"The workstation display has been visually annotated with Set-of-Mark badges ([1], [2], etc.):\n"
        f"{manifest}\n\n"
        f"Refer to elements by their mark ID [N] when explaining or suggesting interactions."
    )
    analysis = _analyze_screen_with_gemini(img_bytes, analysis_prompt)
    if analysis:
        return f"{analysis}\n\n{manifest}"

    return (
        f"Workstation screen annotated with Set-of-Mark badges for '{active_title}', Sir ({len(marks)} marks).\n\n"
        f"{manifest}\n\n"
        f"You may invoke `click_marked_element(mark_id)` to interact with any marked control deterministically."
    )


@SkillRegistry.register(
    name="click_marked_element",
    description="Deterministically clicks an interactive UI element by its Set-of-Mark ID ([1], [2], etc.) identified during inspect_screen_with_marks."
)
def click_marked_element(mark_id: int) -> str:
    """Clicks a marked UI element after verifying target window foreground focus and bounding rect stability."""
    from jarvis.vision.som_grounding import SetOfMarkAnnotator, MarkRegistry
    mark = MarkRegistry.get_mark(mark_id)
    if not mark:
        return f"Mark ID [{mark_id}] was not found in the active mark registry, Sir. Please call inspect_screen_with_marks to refresh element coordinates."

    res = SetOfMarkAnnotator.click_element(mark_id, verify_window_geometry=True)
    if res.get("status") == "clicked":
        name_desc = f" '{mark.name}'" if mark.name else ""
        return (
            f"Successfully clicked mark [{mark_id}] ({mark.control_type}{name_desc}) "
            f"via {res.get('method', 'native UIA')}, Sir."
        )
    return res.get("error", f"Click execution error on mark [{mark_id}], Sir: unknown error")

