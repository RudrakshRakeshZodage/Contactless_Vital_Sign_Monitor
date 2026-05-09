# 🫀 Contactless Vital Sign Monitor

Real-time, **contactless** heart rate & respiration rate estimation from a standard webcam.

## Techniques Used
| Module | Techniques |
|---|---|
| Face Detection | **OpenCV Haar-cascades** (optimized with 2x downscaling for performance) |
| Signal Amplification | **Eulerian Video Magnification (EVM)** – 4x downscaled Laplacian pyramid for high FPS |
| rPPG | **POS** + **CHROM** fusion for robust pulse extraction |
| Respiration | **RBCG** – Tracking nose-tip displacement |
| Filtering | **4th-order Butterworth bandpass** + **Kalman smoother** + **startup guards** |
| Frequency Analysis | **FFT** with zero-padding + **parabolic interpolation** |

## Setup
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py                   # default webcam (index 0)
python main.py --source 1        # alternate webcam
python main.py --source video.mp4
```

## Project Structure
```
LIVE_BP/
├── main.py         ← Dashboard (PyQt5 + PyQtGraph)
├── processor.py    ← Face detection, ROI, EVM
├── signals.py      ← POS, CHROM, Kalman, FFT, filters
└── requirements.txt
```

## Tips for Best Accuracy
- Use in **good, even lighting** (avoid backlighting)
- Sit **still** – reduce head motion
- Keep face **30–60 cm** from the camera
- Allow **~10 seconds** of buffering before readings stabilise
