# Video Knowledge Extraction

An auditable, model-agnostic workflow for turning instructional videos into timestamped visual evidence and transcripts. It provides media probing, decoded-frame extraction, optional perceptual deduplication, subtitle alignment, audio export, and an optional local Ollama vision-language model adapter.

The project treats extracted frames and model answers as evidence to inspect. It does not claim that dense sampling is lossless, that a model permanently learns from an inference prompt, or that one pipeline works best for every video.

## Research status

This repository accompanies a technical report and a single-tutorial case illustration. The available case record lists 50 sampled timestamps and 17 manually retained keyframes (a 66% reduction among the sampled candidates). The original video file, all 50 candidate images, an ASR transcript, and model evaluations were not provided. Therefore the case supports a workflow demonstration only; it cannot establish knowledge retention, accuracy, or a general advantage over other sampling methods.

The paper proposes an evaluation protocol that can be run when licensed source videos, complete frame samples, time-coded transcripts, and human-checked knowledge questions are available. Current related work already studies video knowledge acquisition, frame-rate effects, and adaptive keyframe selection. This project makes a bounded reproducibility contribution and does not claim those ideas as new.

## Requirements

- Python 3.10 or later
- FFmpeg and FFprobe on PATH
- Optional: a local Ollama server and a vision-language model for the learn command
- Optional: an external speech-recognition system; this tool extracts audio but does not perform ASR

The Python package has no third-party runtime dependencies.

## Install

~~~bash
python -m pip install -e .
vkx --help
~~~

## Workflow

1. Inspect container and stream metadata:

   ~~~bash
   vkx probe lesson.mp4
   ~~~

   The report includes nominal and average FPS, duration, codec, dimensions, and reported bitrates where present. A container bitrate may include audio and muxing overhead; it is not the same as a video-stream bitrate.

2. Decode every source frame and preserve timestamped PNGs:

   ~~~bash
   vkx extract lesson.mp4 --out runs/lesson
   ~~~

   This is the default. It decodes frames at the source presentation cadence. For a lower-rate comparison, pass --sample-fps 1. A requested sampling rate above the estimated source rate is rejected; omit the option to decode every source frame.

3. Optionally mark near-duplicate frames without deleting the decoded PNGs:

   ~~~bash
   vkx extract lesson.mp4 --out runs/lesson-dedup --dedupe-hamming 4
   ~~~

   The threshold uses a 64-bit difference hash over a 9×8 grayscale thumbnail. It is a lossy selection heuristic: small text, cursor movement, and subtle state changes may be missed. The frames/ directory remains intact; manifest.json records every candidate and which frames were selected.

4. Align a time-coded SRT or WebVTT file with each extracted frame:

   ~~~bash
   vkx extract lesson.mp4 --out runs/lesson-with-captions --transcript lesson.vtt
   ~~~

   This does not create a transcript. To prepare audio for a separately chosen ASR system:

   ~~~bash
   vkx audio lesson.mp4 --out runs/lesson.wav
   ~~~

5. Optionally ask a local Ollama vision-language model to inspect selected frames:

   ~~~bash
   vkx learn --run-dir runs/lesson-with-captions \
     --model qwen2.5vl:3b \
     --transcript-alignment runs/lesson-with-captions/transcript_alignment.csv
   ~~~

   Model output is saved as knowledge.json and must be checked against source evidence. Use --endpoint to choose the Ollama server; images are sent to that endpoint.

## Outputs

- manifest.json: media metadata, extraction mode, timestamps, exact hashes, perceptual hashes, and selection flags
- frames/frame_*.png: lossless PNG encodings of decoded source frames
- transcript_alignment.csv: per-frame alignment to the closest overlapping or nearby transcript cue, if supplied
- knowledge.json: optional local model responses with frame indices and timestamps

“Lossless” here refers only to preserving the decoded pixel values in PNG files. It does not mean that a sampled subset, perceptual hash, transcript, or model response preserves all semantic knowledge in a video.

## Example case

The case record is in examples/guofeng_loop_border/. It stores timestamp/status annotations and a citation to the public tutorial page. It intentionally excludes the source video and screenshots from the video. The tutorial and its visual material remain subject to their original rights.

## Paper

The accompanying manuscript is in paper/main.tex. It describes prior work, defines an evidence-coverage evaluation protocol, reports the case metadata, and states the limits of the available data. It is not a peer-reviewed publication.

## Citation

If you use the code, cite the manuscript and repository using CITATION.cff. The GitHub repository is a public software/preprint record; it is not a substitute for peer review or an arXiv identifier.
