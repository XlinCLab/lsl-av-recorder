# Default audio settings
DEFAULT_SAMPLING_RATE = 48000
DEFAULT_BIT_DEPTH = 32
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

# GUI BitDepth -> PortAudio capture dtype. 32- and 64-bit both capture as float32
# (64-bit is upcast to float64 in software after capture), so they share one probe
BITDEPTH_DTYPES = {
    16: "int16",
    32: "float32",
    64: "float32",
}
