# Default audio settings
DEFAULT_SAMPLING_RATE = 48000
DEFAULT_BIT_DEPTH = 32
# Storage dtype written to the XDF (independent of capture bit depth)
# float32 in [-1, 1] is the convention most audio tooling assumes
DEFAULT_SAMPLE_FORMAT = "float32"
SAMPLE_FORMATS = ("float32", "int16")
DEFAULT_N_CHANNELS = 1

# Candidate sample rates to probe when discovering what an input device actually
# supports; PortAudio has no "list supported rates" call, so each must be probed
# individually via check_input_settings (Pa_IsFormatSupported)
CANDIDATE_SAMPLE_RATES = [
    8000,
    11025,
    16000,
    22050,
    32000,
    44100,
    48000,
    88200,
    96000,
    176400,
    192000,
]

# GUI BitDepth -> PortAudio capture dtype (what resolution is requested from the device)
BITDEPTH_DTYPES = {
    16: "int16",
    32: "float32",
}
BITDEPTH_CONVERSION_FLOAT = 32768.0
BITDEPTH_CONVERSION_INT = int(BITDEPTH_CONVERSION_FLOAT)

# Relative deviation of the measured delivery rate from the configured sample rate beyond which a recording is flagged
RATE_WARN_TOLERANCE = 0.01
RATE_VALIDATION_TOLERANCE = 0.02

# Signal levels (peak) below which a recording is flagged as silent or very quiet,
# and the peak at or above which it is flagged as clipping (full scale is 1.0)
SILENCE_PEAK_DBFS = -60.0
QUIET_PEAK_DBFS = -40.0
CLIPPING_PEAK = 0.999
