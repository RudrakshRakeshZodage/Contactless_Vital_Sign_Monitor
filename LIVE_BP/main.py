"""
main.py — Contactless Vital Sign Monitor (Non-stop live capture)

Features:
  • Non-stop webcam capture in background thread — never pauses
  • Waveform plots update every 100 ms → smooth EKG-style scrolling
  • Scrolling deque for each signal (fixed window, always newest data on right)
  • Live RECORDING indicator (red dot)
  • Heart Rate (BPM), Respiration Rate (Br/min), SpO2-proxy, & PTT displayed

Usage:
    python main.py
    python main.py --source 1
"""

import sys
import time
import argparse
import numpy as np
import cv2
from collections import deque

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel,
                              QHBoxLayout, QVBoxLayout, QFrame, QSizePolicy,
                              QGridLayout)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QImage, QPixmap, QFont

import pyqtgraph as pg

from processor import VitalSignProcessor
from signals import (bandpass_filter, compute_pos, compute_chrom,
                     fuse_signals, estimate_rate_fft, KalmanFilter1D)

# ─── Config ───────────────────────────────────────────────────────────────────
BUFFER_SECONDS   = 15        # rolling buffer fed to FFT
PLOT_SECONDS     = 8         # seconds of data shown in the graph at once
CAM_FPS_TARGET   = 30
PLOT_UPDATE_MS   = 100       # graph refresh rate (100 ms = 10 Hz, smooth)
ANALYSIS_UPDATE_MS = 250     # vital-sign re-estimation rate

HR_LOW_HZ  = 0.7
HR_HIGH_HZ = 4.0
RR_LOW_HZ  = 0.1
RR_HIGH_HZ = 0.5

C = {   # palette
    "bg":      "#080813",
    "panel":   "#0E0E20",
    "border":  "#1A1A3A",
    "accent":  "#00C8FF",
    "green":   "#00E87A",
    "red":     "#FF3D5A",
    "orange":  "#FF9900",
    "purple":  "#BB86FC",
    "yellow":  "#FFD60A",
    "text":    "#D0D0F0",
    "sub":     "#505080",
}


# ─── Live Scrolling Buffer ────────────────────────────────────────────────────

class RollingBuffer:
    """Fixed-length rolling numpy buffer for real-time plotting."""
    def __init__(self, length):
        self.data = np.zeros(length)
        self._len = length

    def push(self, value):
        self.data = np.roll(self.data, -1)
        self.data[-1] = float(value)

    def push_array(self, arr):
        n = len(arr)
        self.data = np.roll(self.data, -n)
        self.data[-n:] = arr

    def get(self):
        return self.data


# ─── Capture Thread ───────────────────────────────────────────────────────────

class CaptureThread(QThread):
    frame_ready = pyqtSignal(object, bool)   # (BGR frame, face_detected)

    def __init__(self, source, processor):
        super().__init__()
        self.source    = source
        self.processor = processor
        self._running  = True
        self.fps       = float(CAM_FPS_TARGET)
        self._fps_buf  = deque(maxlen=30)

    def run(self):
        # PERFORMANCE: CAP_DSHOW is often the most stable/fast backend for Windows webcams
        cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS,          CAM_FPS_TARGET)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # ← reduce latency

        t_prev = time.perf_counter()
        while self._running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01)
                continue
            t_now = time.perf_counter()
            self._fps_buf.append(1.0 / max(t_now - t_prev, 1e-6))
            self.fps = float(np.mean(self._fps_buf))
            t_prev = t_now

            annotated = self.processor.process_frame(frame)
            self.frame_ready.emit(annotated, self.processor.face_detected)

        cap.release()

    def stop(self):
        self._running = False
        self.wait()


# ─── Styled Widgets ───────────────────────────────────────────────────────────

class LivePlot(pg.PlotWidget):
    """Scrolling EKG-style real-time waveform plot."""
    def __init__(self, title, color, y_label="", fill=False):
        super().__init__()
        self.setBackground(C["panel"])
        self.setTitle(f"  {title}", color=C["text"], size="9pt")
        self.setLabel("left", y_label, color=C["sub"], size="8pt")
        self.showGrid(x=False, y=True, alpha=0.12)
        self.getAxis("bottom").setStyle(showValues=False)
        self.getAxis("left").setTextPen(C["sub"])
        self.getPlotItem().setContentsMargins(2, 2, 2, 2)
        pen = pg.mkPen(color=color, width=2)
        self._curve = self.plot(pen=pen)
        if fill:
            self._curve.setFillLevel(0)
            self._curve.setBrush(pg.mkBrush(color + "22"))
        self.setMinimumHeight(90)
        # Current value label inside the plot
        self._peak_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(color, width=1,
                                                                  style=Qt.DashLine))
        self.addItem(self._peak_line)

    def update_data(self, data):
        if len(data) > 1:
            self._curve.setData(np.arange(len(data)), data)
            self._peak_line.setValue(0)


def make_card(label, unit, val_color, sub_label=""):
    """Flat metric card."""
    frame = QFrame()
    frame.setStyleSheet(f"""
        QFrame {{
            background: {C['panel']};
            border: 1px solid {val_color}40;
            border-radius: 12px;
        }}
    """)
    box = QVBoxLayout(frame)
    box.setContentsMargins(14, 10, 14, 10)
    box.setSpacing(1)

    top = QLabel(label)
    top.setFont(QFont("Segoe UI", 8))
    top.setStyleSheet(f"color:{C['sub']}; background:transparent;")
    top.setAlignment(Qt.AlignCenter)

    val = QLabel("--")
    val.setFont(QFont("Segoe UI Semibold", 32, QFont.Bold))
    val.setStyleSheet(f"color:{val_color}; background:transparent;")
    val.setAlignment(Qt.AlignCenter)

    btm = QLabel(unit)
    btm.setFont(QFont("Segoe UI", 8))
    btm.setStyleSheet(f"color:{C['sub']}; background:transparent;")
    btm.setAlignment(Qt.AlignCenter)

    box.addWidget(top)
    box.addWidget(val)
    box.addWidget(btm)
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return frame, val


# ─── Main Window ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, source):
        super().__init__()
        self.setWindowTitle("🫀 Live Vital Sign Monitor — Non-Stop Capture")
        self.source = source

        self.processor   = VitalSignProcessor(buffer_seconds=BUFFER_SECONDS,
                                              fps=CAM_FPS_TARGET)
        self.kalman_hr   = KalmanFilter1D(0.5,  5.0)
        self.kalman_rr   = KalmanFilter1D(0.1,  3.0)

        # Rolling plot buffers (PLOT_SECONDS * FPS samples shown at once)
        plot_len = PLOT_SECONDS * CAM_FPS_TARGET
        self._pulse_buf  = RollingBuffer(plot_len)
        self._resp_buf   = RollingBuffer(plot_len)
        self._spo2_buf   = RollingBuffer(plot_len)
        self._fft_buf    = RollingBuffer(300)

        # History for trend sparklines
        self._hr_history  = deque(maxlen=120)
        self._rr_history  = deque(maxlen=120)

        # Live values
        self._live_hr  = 0.0
        self._live_rr  = 0.0
        self._live_spo2 = 0.0
        self._rec_tick = 0   # for blinking REC dot

        self._setup_ui()
        self._start_capture()

        # Plot refresh: every 100 ms
        self._plot_timer = QTimer()
        self._plot_timer.timeout.connect(self._refresh_plots)
        self._plot_timer.start(PLOT_UPDATE_MS)

        # Analysis: every 500 ms
        self._analysis_timer = QTimer()
        self._analysis_timer.timeout.connect(self._analyse)
        self._analysis_timer.start(ANALYSIS_UPDATE_MS)

        # Clock: every second
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start(1000)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setStyleSheet(f"background:{C['bg']}; color:{C['text']};")
        self.resize(1300, 780)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # ── LEFT: camera ──────────────────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(6)

        # Header bar
        hdr = QHBoxLayout()
        self._rec_label = QLabel("🔴 LIVE")
        self._rec_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._rec_label.setStyleSheet(f"color:{C['red']};")
        self._time_label = QLabel("00:00")
        self._time_label.setFont(QFont("Segoe UI Semibold", 10))
        self._time_label.setStyleSheet(f"color:{C['sub']};")
        self._fps_disp = QLabel("-- fps")
        self._fps_disp.setFont(QFont("Segoe UI", 10))
        self._fps_disp.setStyleSheet(f"color:{C['accent']};")
        hdr.addWidget(self._rec_label)
        hdr.addStretch()
        hdr.addWidget(self._fps_disp)
        hdr.addSpacing(12)
        hdr.addWidget(self._time_label)

        # Camera frame
        cam_frame = QFrame()
        cam_frame.setStyleSheet(f"background:#000; border-radius:10px; border:1px solid {C['border']};")
        cam_layout = QVBoxLayout(cam_frame)
        cam_layout.setContentsMargins(0, 0, 0, 0)
        self.cam_label = QLabel()
        self.cam_label.setAlignment(Qt.AlignCenter)
        self.cam_label.setMinimumSize(580, 435)
        cam_layout.addWidget(self.cam_label)

        # Status bar
        self.status_label = QLabel("🔍  Waiting for face…")
        self.status_label.setFont(QFont("Segoe UI", 9))
        self.status_label.setStyleSheet(f"color:{C['orange']};")
        self.status_label.setAlignment(Qt.AlignCenter)

        # Metric cards below camera
        cards = QHBoxLayout()
        cards.setSpacing(8)
        _, self.hr_val   = make_card("HEART RATE",   "BPM",     C["red"])
        _, self.rr_val   = make_card("RESPIRATION",  "Br/min",  C["green"])
        _, self.spo2_val = make_card("SpO₂ PROXY",   "%",       C["purple"])
        hr_box,  _       = make_card("HEART RATE",   "BPM",     C["red"])
        rr_box,  _       = make_card("RESPIRATION",  "Br/min",  C["green"])
        spo2_box, _      = make_card("SpO₂ PROXY",   "%",       C["purple"])

        # Rebuild properly with direct references
        hr_card, self.hr_val   = make_card("HEART RATE",  "BPM",    C["red"])
        rr_card, self.rr_val   = make_card("RESPIRATION", "Br/min", C["green"])
        sp_card, self.spo2_val = make_card("SpO₂ PROXY",  "%",      C["purple"])
        cards.addWidget(hr_card)
        cards.addWidget(rr_card)
        cards.addWidget(sp_card)

        left.addLayout(hdr)
        left.addWidget(cam_frame)
        left.addWidget(self.status_label)
        left.addLayout(cards)

        # ── RIGHT: plots ──────────────────────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(6)

        # Title
        title = QLabel("📡  Contactless Vital Sign Monitor — Continuous Capture")
        title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        title.setStyleSheet(f"color:{C['accent']};")
        right.addWidget(title)

        # Plots
        self._pulse_plot = LivePlot("Pulse Wave (rPPG)",       C["red"],    "Amplitude", fill=True)
        self._resp_plot  = LivePlot("Respiration (RBCG)",      C["green"],  "Displacement")
        self._spo2_plot  = LivePlot("SpO₂ Proxy (R/B ratio)",  C["purple"], "Ratio")
        self._fft_plot   = LivePlot("Heart Rate Spectrum (FFT)", C["accent"], "Power")
        self._hr_trend   = LivePlot("HR Trend (last 60 s)",    C["red"],    "BPM")
        self._rr_trend   = LivePlot("RR Trend (last 60 s)",    C["green"],  "Br/min")

        right.addWidget(self._pulse_plot, stretch=3)
        right.addWidget(self._resp_plot,  stretch=2)
        right.addWidget(self._spo2_plot,  stretch=2)
        right.addWidget(self._fft_plot,   stretch=2)

        # Trend row
        trend_row = QHBoxLayout()
        trend_row.setSpacing(6)
        trend_row.addWidget(self._hr_trend)
        trend_row.addWidget(self._rr_trend)
        right.addLayout(trend_row, stretch=2)

        # Footer
        foot = QLabel("Pipeline: Haar Cascade → ROI (Forehead+Cheeks) → EVM → POS+CHROM → Butterworth+Kalman → FFT")
        foot.setFont(QFont("Segoe UI", 7))
        foot.setStyleSheet(f"color:{C['sub']};")
        foot.setAlignment(Qt.AlignCenter)
        right.addWidget(foot)

        root.addLayout(left,  stretch=0)
        root.addLayout(right, stretch=1)

    # ── Capture ───────────────────────────────────────────────────────────────

    def _start_capture(self):
        self.capture_thread = CaptureThread(self.source, self.processor)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.start()

    @pyqtSlot(object, bool)
    def _on_frame(self, frame, face_detected):
        """Display the latest annotated frame (called on every camera frame)."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self.cam_label.setPixmap(
            QPixmap.fromImage(img).scaled(
                self.cam_label.size(), Qt.KeepAspectRatio, Qt.FastTransformation))

        self._fps_disp.setText(f"{self.capture_thread.fps:.1f} fps")

        if face_detected:
            self.status_label.setText("✅  Face locked — capturing all channels non-stop")
            self.status_label.setStyleSheet(f"color:{C['green']};")
        else:
            self.status_label.setText("🔍  Searching for face … please face the camera")
            self.status_label.setStyleSheet(f"color:{C['orange']};")

    # ── Analysis (500 ms) ─────────────────────────────────────────────────────

    def _analyse(self):
        rgb_buf = list(self.processor.rgb_buffer)
        if len(rgb_buf) < 60:
            self.hr_val.setText("Buffering")
            self.rr_val.setText("Buffering")
            self.spo2_val.setText("Buffering")
            return

        rgb_arr = np.array(rgb_buf, dtype=np.float64)
        fs = max(self.capture_thread.fps, 1.0)

        # ── rPPG pulse ────────────────────────────────────────────────────────
        pos  = compute_pos(rgb_arr)
        chrom = compute_chrom(rgb_arr)
        pulse_raw  = fuse_signals(pos, chrom)
        pulse_filt = bandpass_filter(pulse_raw, HR_LOW_HZ, HR_HIGH_HZ, fs)
        pulse_norm = pulse_filt / (np.std(pulse_filt) + 1e-9)

        # Push latest samples into rolling plot buffer
        self._pulse_buf.push_array(pulse_norm[-10:])

        # HR estimate
        hr_raw  = estimate_rate_fft(pulse_filt, fs, HR_LOW_HZ, HR_HIGH_HZ)
        hr_smth = self.kalman_hr.update(hr_raw)
        if 40 < hr_smth < 200:
            self._live_hr = hr_smth
            self._hr_history.append(hr_smth)
            self.hr_val.setText(f"{hr_smth:.0f}")

        # ── SpO₂ proxy (R/B ratio) ────────────────────────────────────────────
        r_mean = rgb_arr[-30:, 0].mean()
        b_mean = rgb_arr[-30:, 2].mean()
        if b_mean > 0:
            ratio = r_mean / b_mean
            # Crude linear mapping: ratio ~0.8–1.5 → SpO2 ~95–100
            spo2 = np.clip(110 - 25 * ratio, 85, 100)
            self._live_spo2 = spo2
            self._spo2_buf.push(ratio)
            self.spo2_val.setText(f"{spo2:.0f}")

        # ── Respiration ───────────────────────────────────────────────────────
        motion_buf = list(self.processor.motion_buffer)
        if len(motion_buf) > 30:
            motion_arr  = np.array(motion_buf, dtype=np.float64)
            motion_filt = bandpass_filter(motion_arr, RR_LOW_HZ, RR_HIGH_HZ, fs)
            rr_raw  = estimate_rate_fft(motion_filt, fs, RR_LOW_HZ, RR_HIGH_HZ)
            rr_smth = self.kalman_rr.update(rr_raw)
            if 6 < rr_smth < 40:
                self._live_rr = rr_smth
                self._rr_history.append(rr_smth)
                self.rr_val.setText(f"{rr_smth:.0f}")
            self._resp_buf.push_array(motion_filt[-10:])

        # ── FFT spectrum ──────────────────────────────────────────────────────
        N = len(pulse_filt)
        fft_mag  = np.abs(np.fft.rfft(pulse_filt * np.hanning(N), n=N * 4))
        fft_freq = np.fft.rfftfreq(N * 4, d=1.0 / fs) * 60
        mask = (fft_freq >= HR_LOW_HZ * 60) & (fft_freq <= HR_HIGH_HZ * 60)
        if mask.any():
            band = fft_mag[mask]
            self._fft_buf = RollingBuffer(len(band))
            self._fft_buf.push_array(band)

    # ── Plot Refresh (100 ms) ─────────────────────────────────────────────────

    def _refresh_plots(self):
        """Fast refresh of all waveforms from rolling buffers."""
        self._pulse_plot.update_data(self._pulse_buf.get())
        self._resp_plot .update_data(self._resp_buf.get())
        self._spo2_plot .update_data(self._spo2_buf.get())
        self._fft_plot  .update_data(self._fft_buf.get())

        if len(self._hr_history) > 1:
            self._hr_trend.update_data(np.array(self._hr_history))
        if len(self._rr_history) > 1:
            self._rr_trend.update_data(np.array(self._rr_history))

    # ── Clock / REC blink ─────────────────────────────────────────────────────

    def _tick(self):
        self._rec_tick += 1
        # Blink the REC dot
        if self._rec_tick % 2 == 0:
            self._rec_label.setText("🔴 LIVE")
            self._rec_label.setStyleSheet(f"color:{C['red']};")
        else:
            self._rec_label.setText("⚫ LIVE")
            self._rec_label.setStyleSheet(f"color:{C['sub']};")

    def closeEvent(self, event):
        self._plot_timer.stop()
        self._analysis_timer.stop()
        self._clock_timer.stop()
        self.capture_thread.stop()
        self.processor.close()
        event.accept()


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=0, help="Camera index or video path")
    args = parser.parse_args()
    try:
        src = int(args.source)
    except ValueError:
        src = args.source

    pg.setConfigOption("background", C["bg"])
    pg.setConfigOption("foreground", C["text"])
    pg.setConfigOptions(antialias=True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow(src)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
