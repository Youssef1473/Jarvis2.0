"""Jarvis 3.0 - Personal AI Assistant."""
import warnings

# Suppress third-party deprecation warnings (e.g. NumPy 2.5 shape assignment in sounddevice)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*Setting the shape on a NumPy array has been deprecated.*")

__version__ = "3.0.0"
