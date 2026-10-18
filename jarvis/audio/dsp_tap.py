"""68-Byte Binary Audio DSP FFT Tap for Jarvis 2.0 Holographic HUD.

Provides zero-copy real-time 64-band frequency visualization and phase tracking
without interfering with the native WASAPI audio playback sink.
"""
import struct
from typing import Optional
import numpy as np


MAGIC_HEADER_BYTE: int = 0x4A  # ASCII 'J'
FFT_WINDOW_SIZE: int = 512
NUM_FFT_BANDS: int = 64


def compute_68byte_dsp_frame(pcm_slice: np.ndarray) -> bytes:
    """Computes a 68-byte binary DSP frame from a float32 PCM window (512 samples).

    Frame Structure (68 bytes total):
    - Byte 0: 0x4A (magic header byte)
    - Byte 1: RMS amplitude (uint8, 0..255)
    - Byte 2: Zero-Crossing Rate (uint8, 0..255)
    - Byte 3: Dominant formant frequency bin index (uint8, 0..63)
    - Bytes 4..67: 64 logarithmic band magnitudes (uint8[64])
    """
    if pcm_slice is None or len(pcm_slice) == 0:
        # Return silent frame
        return bytes([MAGIC_HEADER_BYTE, 0, 0, 0]) + (b"\x00" * NUM_FFT_BANDS)

    # Convert to float32 1D array
    if not isinstance(pcm_slice, np.ndarray):
        pcm_slice = np.asarray(pcm_slice, dtype=np.float32)
    elif pcm_slice.dtype != np.float32:
        pcm_slice = pcm_slice.astype(np.float32)

    if pcm_slice.ndim > 1:
        pcm_slice = pcm_slice[:, 0]

    # Enforce window size (512 samples)
    if len(pcm_slice) < FFT_WINDOW_SIZE:
        pcm_window = np.pad(pcm_slice, (0, FFT_WINDOW_SIZE - len(pcm_slice)), mode="constant")
    elif len(pcm_slice) > FFT_WINDOW_SIZE:
        pcm_window = pcm_slice[:FFT_WINDOW_SIZE]
    else:
        pcm_window = pcm_slice

    # 1. RMS amplitude calculation (0..255)
    rms_val = float(np.sqrt(np.mean(pcm_window ** 2)))
    # Scale RMS: 0.0 -> 0, ~0.4 -> 255
    rms_uint8 = int(np.clip(rms_val * 637.5, 0.0, 255.0))

    # 2. Zero-Crossing Rate (ZCR) calculation (0..255)
    signs = np.sign(pcm_window)
    signs[signs == 0] = 1
    crossings = int(np.sum(signs[:-1] != signs[1:]))
    zcr_ratio = crossings / max(1, len(pcm_window) - 1)
    zcr_uint8 = int(np.clip(zcr_ratio * 255.0, 0.0, 255.0))

    # 3. Hann window attenuation & rFFT (257 bins from 512 samples)
    hann = np.hanning(FFT_WINDOW_SIZE).astype(np.float32)
    windowed = pcm_window * hann
    rfft_mags = np.abs(np.fft.rfft(windowed)).astype(np.float32)  # length 257

    # 4. Map into 64 logarithmic frequency bands (bins 1..256, discarding DC bin 0)
    # Generate 65 logarithmic index boundaries
    log_boundaries = np.geomspace(1.0, 256.0, NUM_FFT_BANDS + 1)
    bands = np.zeros(NUM_FFT_BANDS, dtype=np.float32)

    for i in range(NUM_FFT_BANDS):
        start_idx = int(log_boundaries[i])
        end_idx = max(start_idx + 1, int(round(log_boundaries[i + 1])))
        end_idx = min(end_idx, len(rfft_mags))
        start_idx = min(start_idx, end_idx - 1)
        bands[i] = float(np.mean(rfft_mags[start_idx:end_idx]))

    # Normalize bands into uint8 [0..255]
    peak_band = float(np.max(bands))
    if peak_band > 1e-6:
        # Dynamic normalization with headroom scaling
        norm_bands = np.clip((bands / peak_band) * 255.0, 0.0, 255.0).astype(np.uint8)
    else:
        norm_bands = np.zeros(NUM_FFT_BANDS, dtype=np.uint8)

    # 5. Dominant formant frequency bin index (0..63)
    dominant_idx = int(np.argmax(norm_bands)) % NUM_FFT_BANDS

    # 6. Pack 68-byte binary frame
    header = struct.pack("4B", MAGIC_HEADER_BYTE, rms_uint8, zcr_uint8, dominant_idx)
    frame = header + norm_bands.tobytes()

    if len(frame) != 68:
        raise ValueError(f"DSP frame size mismatch: expected 68 bytes, got {len(frame)}")

    return frame


class AudioDspTap:
    """Thread-safe tap connecting WASAPI audio stream slices to the HUD WebSocket broadcaster."""

    _instance: Optional["AudioDspTap"] = None

    def __init__(self):
        self._enabled: bool = True

    @classmethod
    def get_instance(cls) -> "AudioDspTap":
        if cls._instance is None:
            cls._instance = AudioDspTap()
        return cls._instance

    def process_and_broadcast(self, pcm_chunk: np.ndarray) -> Optional[bytes]:
        """Computes a 68-byte DSP frame and dispatches it to connected UI clients."""
        if not self._enabled or pcm_chunk is None or len(pcm_chunk) == 0:
            return None

        try:
            frame = compute_68byte_dsp_frame(pcm_chunk)
            from jarvis.ui.server import JarvisUIService
            service = JarvisUIService.get_instance()
            if service and service.connected_clients:
                service.broadcast_dsp_frame(frame)
            return frame
        except Exception:
            return None

    def reset(self) -> None:
        """Resets the DSP tap state and clears cached references."""
        self._enabled = True

