"""
signals.py — Signal processing module for Contactless Vital Sign Monitoring
Implements:
- Bandpass filtering (Butterworth)
- Kalman filtering
- FFT-based frequency estimation
- POS (Plane-Orthogonal-to-Skin) rPPG algorithm
- CHROM (Chrominance-based) rPPG algorithm
"""

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks


# ─── Filter Bank ──────────────────────────────────────────────────────────────

def butter_bandpass(lowcut, highcut, fs, order=4):
    """Design a Butterworth bandpass filter."""
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    low = max(1e-5, min(low, 0.9999))
    high = max(1e-5, min(high, 0.9999))
    b, a = butter(order, [low, high], btype='band')
    return b, a


def bandpass_filter(signal, lowcut, highcut, fs, order=4):
    """Apply bandpass filter to a 1D signal."""
    # filtfilt needs at least padlen+1 samples; padlen = 3*max(len(a),len(b))
    # For a 4th-order Butterworth bandpass, len(a)=len(b)=9, padlen=27
    if len(signal) < 36:
        return signal
    try:
        b, a = butter_bandpass(lowcut, highcut, fs, order=order)
        return filtfilt(b, a, signal)
    except Exception:
        return signal


# ─── Kalman Filter ────────────────────────────────────────────────────────────

class KalmanFilter1D:
    """Simple 1D Kalman filter for signal smoothing."""

    def __init__(self, process_variance=1e-3, measurement_variance=0.1):
        self.process_variance = process_variance
        self.measurement_variance = measurement_variance
        self.estimate = 0.0
        self.estimate_error = 1.0

    def update(self, measurement):
        # Prediction step
        prediction = self.estimate
        prediction_error = self.estimate_error + self.process_variance

        # Update step
        kalman_gain = prediction_error / (prediction_error + self.measurement_variance)
        self.estimate = prediction + kalman_gain * (measurement - prediction)
        self.estimate_error = (1 - kalman_gain) * prediction_error
        return self.estimate


# ─── rPPG Algorithms ─────────────────────────────────────────────────────────

def compute_pos(rgb_signal):
    """
    POS (Plane-Orthogonal-to-Skin) algorithm.
    Wang, W. et al., IEEE Trans. Biomed. Eng., 2017.

    Args:
        rgb_signal: ndarray of shape (N, 3), columns = [R, G, B]
    Returns:
        1D pulse signal
    """
    rgb = np.array(rgb_signal, dtype=np.float64)
    if rgb.shape[0] < 10:
        return np.zeros(rgb.shape[0])

    # Normalize each channel by its mean
    mean = rgb.mean(axis=0)
    mean = np.where(mean == 0, 1e-6, mean)
    normalized = rgb / mean

    # POS projection
    # S1 = R - G, S2 = R + G - 2B
    S1 = normalized[:, 0] - normalized[:, 1]
    S2 = normalized[:, 0] + normalized[:, 1] - 2 * normalized[:, 2]

    alpha = np.std(S1) / (np.std(S2) + 1e-6)
    pulse = S1 - alpha * S2
    return pulse


def compute_chrom(rgb_signal):
    """
    CHROM (Chrominance-based) algorithm.
    de Haan, G. & Jeanne, V., IEEE Trans. Biomed. Eng., 2013.

    Args:
        rgb_signal: ndarray of shape (N, 3), columns = [R, G, B]
    Returns:
        1D pulse signal
    """
    rgb = np.array(rgb_signal, dtype=np.float64)
    if rgb.shape[0] < 10:
        return np.zeros(rgb.shape[0])

    # White-balance normalization
    mean = rgb.mean(axis=0)
    mean = np.where(mean == 0, 1e-6, mean)
    normalized = rgb / mean

    Xc = 3 * normalized[:, 0] - 2 * normalized[:, 1]
    Yc = 1.5 * normalized[:, 0] + normalized[:, 1] - 1.5 * normalized[:, 2]

    alpha = np.std(Xc) / (np.std(Yc) + 1e-6)
    pulse = Xc - alpha * Yc
    return pulse


def fuse_signals(sig1, sig2):
    """Combine two pulse signals by weighted average (std-based weights)."""
    w1 = 1.0 / (np.std(sig1) + 1e-6)
    w2 = 1.0 / (np.std(sig2) + 1e-6)
    return (w1 * sig1 + w2 * sig2) / (w1 + w2)


# ─── Frequency Analysis ───────────────────────────────────────────────────────

def estimate_rate_fft(signal, fs, min_hz=0.7, max_hz=4.0):
    """
    Estimate dominant frequency (in BPM) using FFT with peak detection.

    Args:
        signal: 1D array
        fs:     sampling rate in Hz
        min_hz: minimum physiological frequency in Hz (0.7 Hz = 42 BPM)
        max_hz: maximum physiological frequency in Hz (4.0 Hz = 240 BPM)
    Returns:
        Rate in BPM (beats/breaths per minute)
    """
    N = len(signal)
    if N < 10:
        return 0.0

    # Hanning window to reduce spectral leakage
    windowed = signal * np.hanning(N)
    fft_vals = np.abs(np.fft.rfft(windowed, n=N * 4))  # zero-pad 4x
    freqs = np.fft.rfftfreq(N * 4, d=1.0 / fs)

    # Restrict to physiological band
    mask = (freqs >= min_hz) & (freqs <= max_hz)
    if not mask.any():
        return 0.0

    fft_band = fft_vals[mask]
    freqs_band = freqs[mask]

    # Parabolic interpolation around FFT peak
    peak_idx = np.argmax(fft_band)
    if 0 < peak_idx < len(fft_band) - 1:
        # Parabolic peak refinement
        alpha = fft_band[peak_idx - 1]
        beta  = fft_band[peak_idx]
        gamma = fft_band[peak_idx + 1]
        p = 0.5 * (alpha - gamma) / (alpha - 2 * beta + gamma + 1e-9)
        freq_resolution = freqs_band[1] - freqs_band[0]
        dominant_freq = freqs_band[peak_idx] + p * freq_resolution
    else:
        dominant_freq = freqs_band[peak_idx]

    return dominant_freq * 60.0  # Hz → BPM or BrPM
