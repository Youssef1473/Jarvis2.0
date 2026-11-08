import sounddevice as sd

print(f"Default devices (in, out): {sd.default.device}")
hostapis = sd.query_hostapis()
print("\n--- Host APIs ---")
for i, h in enumerate(hostapis):
    print(f"API {i}: {h['name']} (default in: {h['default_input_device']}, out: {h['default_output_device']})")

devices = sd.query_devices()
print("\n--- Audio Devices ---")
for i, d in enumerate(devices):
    api_name = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else "Unknown"
    in_ch = d["max_input_channels"]
    out_ch = d["max_output_channels"]
    sr = d["default_samplerate"]
    print(f"Device [{i:2d}] (API: {api_name:<18}): In={in_ch}, Out={out_ch}, SR={int(sr)} | {d['name']}")

