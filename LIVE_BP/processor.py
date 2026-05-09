"""
processor.py — GPU-Accelerated Video Processing & Signal Extraction
Uses OpenCV for face detection (CPU) and PyTorch+CUDA for Eulerian Magnification.

Optimized for RTX 4060:
  • Full-resolution EVM processing on GPU.
  • Temporal bandpass filtering via 1D Convolution on CUDA.
  • Laplacian pyramids implemented with PyTorch functionals.
"""

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from collections import deque

# ─── Cascade Download ─────────────────────────────────────────────────────────
_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


class GPUEulerianMagnifier:
    """Temporal colour EVM implemented on GPU using PyTorch."""

    def __init__(self, buffer_size=64, alpha=30.0,
                 low_hz=1.0, high_hz=2.0, fs=30.0, levels=2):
        self.device    = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.alpha     = alpha
        self.low_hz    = low_hz
        self.high_hz   = high_hz
        self.fs        = fs
        self.levels    = levels
        self.buffer_size = buffer_size
        
        # Buffer on GPU: (T, C, H, W)
        self._buffer = None 
        
        # Pre-calculate bandpass filter coefficients for 1D convolution
        # We'll use a simple temporal window (Difference of Boxes) for speed
        self._filter_kernel = self._make_bandpass_kernel(buffer_size, low_hz, high_hz, fs)

    def _make_bandpass_kernel(self, size, low, high, fs):
        """Creates a 1D convolution kernel for temporal bandpass filtering."""
        # A simple sinc-based FIR filter
        t = torch.arange(size, device=self.device) - (size - 1) / 2
        
        # Normalized frequencies
        w_low  = 2 * np.pi * low / fs
        w_high = 2 * np.pi * high / fs
        
        # Sinc filter
        def sinc(x):
            return torch.where(x == 0, torch.ones_like(x), torch.sin(x) / x)
        
        kernel = (sinc(w_high * t) * w_high / np.pi) - (sinc(w_low * t) * w_low / np.pi)
        kernel *= torch.hamming_window(size, device=self.device) # Windowing
        return kernel.view(1, 1, -1) # (Out, In, Width)

    def _pyramid_down(self, x):
        return F.avg_pool2d(x, kernel_size=2, stride=2)

    def _pyramid_up(self, x, target_size):
        return F.interpolate(x, size=target_size, mode='bilinear', align_corners=False)

    def _get_laplacian_pyramid(self, x):
        pyr = []
        cur = x
        for _ in range(self.levels):
            dn = self._pyramid_down(cur)
            up = self._pyramid_up(dn, (cur.shape[2], cur.shape[3]))
            pyr.append(cur - up)
            cur = dn
        pyr.append(cur)
        return pyr

    def _reconstruct(self, pyr):
        cur = pyr[-1]
        for lv in reversed(pyr[:-1]):
            cur = self._pyramid_up(cur, (lv.shape[2], lv.shape[3])) + lv
        return cur

    def push(self, frame_bgr, face_rect=None):
        """Processes frame. If face_rect is None, skips EVM."""
        if face_rect is None:
            return frame_bgr
            
        fx, fy, fw, fh = face_rect
        roi_bgr = frame_bgr[fy:fy+fh, fx:fx+fw]
        if roi_bgr.size == 0:
            return frame_bgr
            
        # Convert to Torch on GPU
        # PERFORMANCE: Always resize Roi to a fixed 128x128 for GPU buffer stability
        target_size = 128
        roi_torch = torch.from_numpy(roi_bgr).to(self.device).permute(2, 0, 1).float().unsqueeze(0) / 255.0
        roi_resized = F.interpolate(roi_torch, size=(target_size, target_size), mode='bilinear', align_corners=False)
        
        # Build pyramid on fixed resolution
        pyr = self._get_laplacian_pyramid(roi_resized)
        target = pyr[0] # (1, 3, 128, 128)
        
        # Manage buffer (now stable at 128x128)
        if self._buffer is None:
            self._buffer = target.repeat(self.buffer_size, 1, 1, 1) # Initialise
        else:
            self._buffer = torch.roll(self._buffer, -1, dims=0)
            self._buffer[-1] = target[0]

        # Apply temporal filtering (1D Conv across time dim 0)
        # Buffer: (T, C, 128, 128) -> (C, 128, 128, T) for 1D conv
        v = self._buffer.permute(1, 2, 3, 0).reshape(-1, 1, self.buffer_size) 
        # Ensure output size matches input size exactly
        filtered_v = F.conv1d(v, self._filter_kernel, padding=self.buffer_size // 2)[:, :, :self.buffer_size]
        filtered = filtered_v.view(3, target_size, target_size, self.buffer_size).permute(3, 0, 1, 2)
        
        # Magnify and reconstruct
        amp_pyr = [p.clone() for p in pyr]
        amp_pyr[0] = amp_pyr[0] + self.alpha * filtered[-1].unsqueeze(0)
        
        res = self._reconstruct(amp_pyr)
        
        # Resize back to original ROI dimensions
        res_scaled = F.interpolate(res, size=(fh, fw), mode='bilinear', align_corners=False)
        res_np = (res_scaled[0].permute(1, 2, 0).clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
        
        # Overlay back
        out = frame_bgr.copy()
        out[fy:fy+fh, fx:fx+fw] = res_np
        return out


class VitalSignProcessor:
    def __init__(self, buffer_seconds=10, fps=30):
        self.fps = fps
        self.buffer_size = buffer_seconds * fps
        self.detector = cv2.CascadeClassifier(_CASCADE_PATH)
        
        self.rgb_buffer = deque(maxlen=self.buffer_size)
        self.motion_buffer = deque(maxlen=self.buffer_size)
        self._prev_roi_y = None
        self._last_face = None
        
        self.evm = GPUEulerianMagnifier(buffer_size=32, fs=fps, alpha=40.0)
        self.face_detected = False

    def _safe_mean_bgr(self, frame_bgr, x, y, w, h):
        roi = frame_bgr[y:y+h, x:x+w]
        if roi.size == 0: return np.zeros(3)
        return roi.reshape(-1, 3).mean(axis=0)[::-1]

    def _extract_rois(self, frame_bgr, fx, fy, fw, fh):
        fh_h, fh_x, fh_w, fh_y = int(fh*0.2), fx+int(fw*0.2), int(fw*0.6), fy+int(fh*0.05)
        lc_y, lc_h, lc_x, lc_w = fy+int(fh*0.4), int(fh*0.3), fx+int(fw*0.05), int(fw*0.25)
        rc_x, rc_w = fx+int(fw*0.7), int(fw*0.25)
        
        rgb_fh = self._safe_mean_bgr(frame_bgr, fh_x, fh_y, fh_w, fh_h)
        rgb_lc = self._safe_mean_bgr(frame_bgr, lc_x, lc_y, lc_w, lc_h)
        rgb_rc = self._safe_mean_bgr(frame_bgr, rc_x, lc_y, rc_w, lc_h)
        
        return (rgb_fh + rgb_lc + rgb_rc) / 3.0, (fh_x, fh_y, fh_w, fh_h, lc_x, lc_y, lc_w, lc_h, rc_x, rc_w)

    def process_frame(self, frame_bgr):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        small_gray = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5)
        faces = self.detector.detectMultiScale(small_gray, 1.2, 5, minSize=(30, 30))
        faces = [[int(x*2), int(y*2), int(w*2), int(h*2)] for (x, y, w, h) in faces]

        self.face_detected = False
        if len(faces):
            fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
            self._last_face = (fx, fy, fw, fh)
            self.face_detected = True
        elif self._last_face is not None:
            fx, fy, fw, fh = self._last_face
            self.face_detected = True
        else:
            return frame_bgr

        # Vitals Signal
        rgb_val, rects = self._extract_rois(frame_bgr, fx, fy, fw, fh)
        self.rgb_buffer.append(rgb_val)
        
        nose_y = fy + int(fh*0.55)
        if self._prev_roi_y is not None:
            self.motion_buffer.append(float(nose_y - self._prev_roi_y))
        self._prev_roi_y = nose_y

        # GPU EVM
        annotated = self.evm.push(frame_bgr, (fx, fy, fw, fh))

        # Overlays
        TEAL, GREEN, ORANGE = (0, 220, 180), (0, 220, 60), (0, 160, 255)
        cv2.rectangle(annotated, (fx, fy), (fx+fw, fy+fh), TEAL, 2)
        fh_x, fh_y, fh_w, fh_h, lc_x, lc_y, lc_w, lc_h, rc_x, rc_w = rects
        cv2.rectangle(annotated, (fh_x, fh_y), (fh_x+fh_w, fh_y+fh_h), GREEN, 1)
        cv2.rectangle(annotated, (lc_x, lc_y), (lc_x+lc_w, lc_y+lc_h), ORANGE, 1)
        cv2.rectangle(annotated, (rc_x, lc_y), (rc_x+rc_w, lc_y+lc_h), ORANGE, 1)
        
        return annotated

    def close(self):
        pass
